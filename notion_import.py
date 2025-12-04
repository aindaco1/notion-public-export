#!/usr/bin/env python3
"""
Import exported markdown + assets back into Notion using the official API.

Requirements:
    pip install notion-client boto3

Setup:
    1. Create an integration at https://www.notion.so/my-integrations
    2. Copy the "Internal Integration Token"
    3. Share a target Notion page with your integration (click ... → Connections → Add your integration)
    4. Get the page ID from the URL (the 32-char hex string after the page name)

For Cloudflare R2 image hosting:
    1. Create an R2 bucket in Cloudflare dashboard
    2. Enable public access (Settings → Public access → Allow Access)
    3. Create an API token: R2 → Manage R2 API Tokens → Create API Token
    4. Copy the Access Key ID, Secret Access Key, and your Account ID

Usage:
    # Without images
    python notion_import.py --token <notion-token> --parent <page-id> --input export/
    
    # With Cloudflare R2
    python notion_import.py --token <notion-token> --parent <page-id> --input export/ \\
        --r2-account-id <account-id> \\
        --r2-access-key <access-key> \\
        --r2-secret-key <secret-key> \\
        --r2-bucket <bucket-name>
"""

import argparse
import base64
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

try:
    from notion_client import Client
except ImportError:
    print("Please install notion-client: pip install notion-client")
    exit(1)

try:
    import boto3
    from botocore.config import Config
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False


class CloudflareR2Uploader:
    """Upload files to Cloudflare R2 for public hosting."""
    
    def __init__(self, account_id: str, access_key: str, secret_key: str, bucket: str, public_url: str = None):
        self.bucket = bucket
        self.public_url_base = public_url.rstrip("/") if public_url else None
        
        # R2 endpoint
        endpoint_url = f"https://{account_id}.r2.cloudflarestorage.com"
        
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            config=Config(signature_version="s3v4"),
        )
    
    def set_public_url(self, url: str):
        """Set public URL base (from R2 bucket settings or custom domain)."""
        self.public_url_base = url.rstrip("/")
    
    def upload(self, file_path: Path, key: str = None) -> Optional[str]:
        """Upload a file to R2 and return its public URL."""
        file_path = Path(file_path)
        if not file_path.exists():
            return None
        
        if not self.public_url_base:
            print(f"[error] No public URL configured. Use --r2-public-url with the URL from your R2 bucket settings.")
            return None
        
        if key is None:
            key = f"notion-import/{file_path.name}"
        
        # Determine content type
        content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
        
        try:
            with open(file_path, "rb") as f:
                self.s3.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=f,
                    ContentType=content_type,
                )
            
            return f"{self.public_url_base}/{key}"
        except Exception as e:
            print(f"[warn] R2 upload failed for {file_path}: {e}")
            return None


class NotionImporter:
    def __init__(self, token: str, parent_page_id: str, delay: float = 0.35):
        self.client = Client(auth=token)
        self.parent_page_id = self._normalize_id(parent_page_id)
        self.delay = delay
        self.created_pages: Dict[str, str] = {}  # local path -> notion page id
        self.file_uploader: Optional[CloudflareR2Uploader] = None
        
        # For internal file uploads
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": "2022-06-28",
        })
    
    def set_r2_uploader(self, uploader: CloudflareR2Uploader):
        """Set Cloudflare R2 uploader for image hosting."""
        self.file_uploader = uploader

    @staticmethod
    def _normalize_id(page_id: str) -> str:
        """Normalize page ID to UUID format."""
        # Remove any URL parts
        if "/" in page_id:
            page_id = page_id.split("/")[-1]
        # Remove any query params
        if "?" in page_id:
            page_id = page_id.split("?")[0]
        # Look for 32-char hex string (may be at end after page name)
        match = re.search(r'([0-9a-fA-F]{32})$', page_id)
        if match:
            raw = match.group(1).lower()
            return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"
        # Try hyphenated UUID format
        match_uuid = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', page_id)
        if match_uuid:
            return match_uuid.group(1).lower()
        return page_id

    def _rate_limit(self):
        time.sleep(self.delay)

    # --- Markdown Parsing ---

    def _parse_markdown(self, md_content: str, assets_dir: Path) -> List[Dict[str, Any]]:
        """Convert markdown content to Notion blocks."""
        blocks = []
        lines = md_content.split("\n")
        i = 0
        
        while i < len(lines):
            line = lines[i]
            
            # Skip empty lines
            if not line.strip():
                i += 1
                continue
            
            # Headers
            if line.startswith("# "):
                blocks.append(self._heading_block(line[2:].strip(), 1))
            elif line.startswith("## "):
                blocks.append(self._heading_block(line[3:].strip(), 2))
            elif line.startswith("### "):
                blocks.append(self._heading_block(line[4:].strip(), 3))
            
            # Divider
            elif line.strip() == "---":
                blocks.append({"type": "divider", "divider": {}})
            
            # Bullet list
            elif line.strip().startswith("- "):
                indent = len(line) - len(line.lstrip())
                text = line.strip()[2:]
                blocks.append(self._bullet_block(text))
            
            # Numbered list
            elif re.match(r"^\s*\d+\.\s", line):
                text = re.sub(r"^\s*\d+\.\s", "", line)
                blocks.append(self._numbered_block(text))
            
            # Quote or Callout (quote with emoji at start)
            elif line.startswith("> "):
                quote_text = line[2:].strip()
                # Check if it starts with an emoji (callout)
                emoji_match = re.match(r'^([\U0001F300-\U0001F9FF]|➡️|💡|⚠️|❗|✅|❌|📝|🔗|📌|🎯|💭|💬|📢|🔔|⭐|🚀|💪|👉|👆|👇|✨|🎉|🎊|🔥|💯|🏆|🎁|📚|📖|🔒|🔓|⏰|📅|📆)\s*(.*)', quote_text)
                if emoji_match:
                    icon = emoji_match.group(1)
                    text = emoji_match.group(2)
                    blocks.append(self._callout_block(text, icon))
                else:
                    blocks.append(self._quote_block(quote_text))
            
            # Code block
            elif line.startswith("```"):
                lang = line[3:].strip()
                code_lines = []
                i += 1
                while i < len(lines) and not lines[i].startswith("```"):
                    code_lines.append(lines[i])
                    i += 1
                blocks.append(self._code_block("\n".join(code_lines), lang))
            
            # HTML Table (column layout)
            elif line.strip().startswith("<table>"):
                # Collect all table content until </table>
                table_lines = [line]
                i += 1
                while i < len(lines) and "</table>" not in lines[i-1]:
                    table_lines.append(lines[i])
                    i += 1
                table_html = "\n".join(table_lines)
                column_blocks = self._parse_table_columns(table_html, assets_dir)
                if column_blocks:
                    blocks.append(column_blocks)
                continue  # Already incremented i
            
            # HTML Details/Summary (toggle block)
            elif line.strip().startswith("<details>"):
                # Collect all content until </details>
                details_lines = [line]
                i += 1
                depth = 1
                while i < len(lines) and depth > 0:
                    if "<details>" in lines[i]:
                        depth += 1
                    if "</details>" in lines[i]:
                        depth -= 1
                    details_lines.append(lines[i])
                    i += 1
                details_html = "\n".join(details_lines)
                toggle_block = self._parse_toggle(details_html, assets_dir)
                if toggle_block:
                    blocks.append(toggle_block)
                continue  # Already incremented i
            
            # Image
            elif line.strip().startswith("!["):
                match = re.match(r"!\[([^\]]*)\]\(([^)]+)\)", line.strip())
                if match:
                    alt_text = match.group(1)
                    src = match.group(2)
                    block = self._image_block(src, alt_text, assets_dir)
                    if block:
                        blocks.append(block)
            
            # Link (video/audio embeds we converted to links)
            elif line.strip().startswith("[▶") or line.strip().startswith("[🔊"):
                match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", line.strip())
                if match:
                    text = match.group(1)
                    url = match.group(2)
                    blocks.append(self._embed_block(url))
            
            # Regular link on its own line
            elif re.match(r"^\[.+\]\(.+\)$", line.strip()):
                match = re.match(r"\[([^\]]+)\]\(([^)]+)\)", line.strip())
                if match:
                    text = match.group(1)
                    url = match.group(2)
                    if url.startswith("http"):
                        blocks.append(self._bookmark_block(url))
                    else:
                        # Internal link - just render as text for now
                        blocks.append(self._paragraph_block(f"{text}"))
            
            # Regular paragraph
            else:
                # Collect consecutive non-empty, non-special lines as one paragraph
                para_lines = [line]
                while (i + 1 < len(lines) and 
                       lines[i + 1].strip() and 
                       not lines[i + 1].startswith("#") and
                       not lines[i + 1].startswith("-") and
                       not lines[i + 1].startswith(">") and
                       not lines[i + 1].startswith("```") and
                       not lines[i + 1].startswith("![") and
                       not lines[i + 1].strip() == "---"):
                    i += 1
                    para_lines.append(lines[i])
                
                text = " ".join(para_lines)
                if text.strip():
                    blocks.append(self._paragraph_block(text))
            
            i += 1
        
        return blocks

    # Map CSS colors to Notion color names
    CSS_TO_NOTION_COLOR = {
        "#787774": "gray",
        "#9F6B53": "brown", 
        "#D9730D": "orange",
        "#CB912F": "yellow",
        "#448361": "green",
        "#337EA9": "blue",
        "#9065B0": "purple",
        "#C14C8A": "pink",
        "#D44C47": "red",
        "#F1F1EF": "gray_background",
        "#F4EEEE": "brown_background",
        "#FBECDD": "orange_background",
        "#FBF3DB": "yellow_background",
        "#EDF3EC": "green_background",
        "#E7F3F8": "blue_background",
        "#F6F3F9": "purple_background",
        "#FAF1F5": "pink_background",
        "#FDEBEC": "red_background",
    }

    def _rich_text(self, text: str) -> List[Dict[str, Any]]:
        """Convert text to Notion rich text format with formatting."""
        if not text:
            return []
        
        segments = []
        self._parse_rich_text_recursive(text, segments, {})
        return segments if segments else [{"type": "text", "text": {"content": text}}]
    
    def _parse_rich_text_recursive(self, text: str, segments: List, annotations: Dict):
        """Recursively parse text with nested formatting."""
        if not text:
            return
        
        # Check for color spans: <span style="color: #xxx">...</span>
        color_match = re.match(r'<span style="color:\s*([^"]+)">(.*?)</span>(.*)', text, re.DOTALL)
        if color_match:
            color_css = color_match.group(1).strip()
            inner_text = color_match.group(2)
            remaining = color_match.group(3)
            
            notion_color = self.CSS_TO_NOTION_COLOR.get(color_css, "default")
            new_annotations = {**annotations, "color": notion_color}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for background color spans
        bg_match = re.match(r'<span style="background-color:\s*([^"]+)">(.*?)</span>(.*)', text, re.DOTALL)
        if bg_match:
            color_css = bg_match.group(1).strip()
            inner_text = bg_match.group(2)
            remaining = bg_match.group(3)
            
            notion_color = self.CSS_TO_NOTION_COLOR.get(color_css, "default")
            new_annotations = {**annotations, "color": notion_color}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for underline: <u>...</u>
        underline_match = re.match(r'<u>(.*?)</u>(.*)', text, re.DOTALL)
        if underline_match:
            inner_text = underline_match.group(1)
            remaining = underline_match.group(2)
            new_annotations = {**annotations, "underline": True}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for bold+italic: ***...***
        bold_italic_match = re.match(r'\*\*\*(.+?)\*\*\*(.*)', text, re.DOTALL)
        if bold_italic_match:
            inner_text = bold_italic_match.group(1)
            remaining = bold_italic_match.group(2)
            new_annotations = {**annotations, "bold": True, "italic": True}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for bold: **...**
        bold_match = re.match(r'\*\*(.+?)\*\*(.*)', text, re.DOTALL)
        if bold_match:
            inner_text = bold_match.group(1)
            remaining = bold_match.group(2)
            new_annotations = {**annotations, "bold": True}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for italic: *...*
        italic_match = re.match(r'\*(.+?)\*(.*)', text, re.DOTALL)
        if italic_match:
            inner_text = italic_match.group(1)
            remaining = italic_match.group(2)
            new_annotations = {**annotations, "italic": True}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for strikethrough: ~~...~~
        strike_match = re.match(r'~~(.+?)~~(.*)', text, re.DOTALL)
        if strike_match:
            inner_text = strike_match.group(1)
            remaining = strike_match.group(2)
            new_annotations = {**annotations, "strikethrough": True}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for inline code: `...`
        code_match = re.match(r'`([^`]+)`(.*)', text, re.DOTALL)
        if code_match:
            inner_text = code_match.group(1)
            remaining = code_match.group(2)
            new_annotations = {**annotations, "code": True}
            self._parse_rich_text_recursive(inner_text, segments, new_annotations)
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Check for links: [text](url)
        link_match = re.match(r'\[([^\]]+)\]\(([^)]+)\)(.*)', text, re.DOTALL)
        if link_match:
            link_text = link_match.group(1)
            link_url = link_match.group(2)
            remaining = link_match.group(3)
            
            # Parse the link text for any nested formatting
            link_segments = []
            self._parse_rich_text_recursive(link_text, link_segments, annotations)
            
            # Add link to each segment
            for seg in link_segments:
                if link_url.startswith("http"):
                    seg["text"]["link"] = {"url": link_url}
                segments.append(seg)
            
            self._parse_rich_text_recursive(remaining, segments, annotations)
            return
        
        # Find next special character
        special_chars = ['<', '*', '~', '`', '[']
        next_special = len(text)
        for char in special_chars:
            pos = text.find(char)
            if pos != -1 and pos < next_special:
                next_special = pos
        
        if next_special > 0:
            # Add plain text up to the special char
            plain_text = text[:next_special]
            segment = {"type": "text", "text": {"content": plain_text}}
            if annotations:
                segment["annotations"] = annotations
            segments.append(segment)
            self._parse_rich_text_recursive(text[next_special:], segments, annotations)
        elif next_special == 0:
            # Special char at start but didn't match patterns - treat as plain
            segment = {"type": "text", "text": {"content": text[0]}}
            if annotations:
                segment["annotations"] = annotations
            segments.append(segment)
            self._parse_rich_text_recursive(text[1:], segments, annotations)
        else:
            # No special chars - add all as plain text
            segment = {"type": "text", "text": {"content": text}}
            if annotations:
                segment["annotations"] = annotations
            segments.append(segment)

    def _heading_block(self, text: str, level: int) -> Dict[str, Any]:
        block_type = f"heading_{level}"
        return {
            "type": block_type,
            block_type: {"rich_text": self._rich_text(text)}
        }

    def _paragraph_block(self, text: str) -> Dict[str, Any]:
        return {
            "type": "paragraph",
            "paragraph": {"rich_text": self._rich_text(text)}
        }

    def _bullet_block(self, text: str) -> Dict[str, Any]:
        return {
            "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": self._rich_text(text)}
        }

    def _numbered_block(self, text: str) -> Dict[str, Any]:
        return {
            "type": "numbered_list_item",
            "numbered_list_item": {"rich_text": self._rich_text(text)}
        }

    def _quote_block(self, text: str) -> Dict[str, Any]:
        return {
            "type": "quote",
            "quote": {"rich_text": self._rich_text(text)}
        }

    def _callout_block(self, text: str, icon: str = "💡") -> Dict[str, Any]:
        return {
            "type": "callout",
            "callout": {
                "rich_text": self._rich_text(text),
                "icon": {"type": "emoji", "emoji": icon}
            }
        }

    def _code_block(self, code: str, language: str = "") -> Dict[str, Any]:
        # Map common language names to Notion's expected values
        lang_map = {
            "js": "javascript",
            "ts": "typescript",
            "py": "python",
            "rb": "ruby",
            "sh": "shell",
            "bash": "shell",
            "": "plain text",
        }
        lang = lang_map.get(language.lower(), language.lower()) or "plain text"
        
        return {
            "type": "code",
            "code": {
                "rich_text": [{"type": "text", "text": {"content": code}}],
                "language": lang
            }
        }

    def _image_block(self, src: str, alt_text: str, assets_dir: Path) -> Optional[Dict[str, Any]]:
        """Create an image block. Handles both URLs and local files."""
        if src.startswith("http"):
            return {
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": src}
                }
            }
        elif src.startswith("data:"):
            # Base64 data URL - skip (Notion doesn't support these directly)
            print(f"[skip] Base64 image (not supported by Notion API)")
            return None
        else:
            # Local file - need to upload
            file_path = assets_dir / src.replace("assets/", "")
            try:
                if file_path.exists():
                    url = self._upload_file(file_path)
                    if url:
                        return {
                            "type": "image",
                            "image": {
                                "type": "external",
                                "external": {"url": url}
                            }
                        }
            except OSError as e:
                print(f"[skip] Invalid file path: {e}")
            return None

    def _embed_block(self, url: str) -> Dict[str, Any]:
        """Create an embed block for videos, etc."""
        # Try video embed first, fall back to bookmark
        if "youtube.com" in url or "youtu.be" in url or "vimeo.com" in url:
            return {
                "type": "video",
                "video": {
                    "type": "external",
                    "external": {"url": url}
                }
            }
        return {
            "type": "embed",
            "embed": {"url": url}
        }

    def _bookmark_block(self, url: str) -> Dict[str, Any]:
        return {
            "type": "bookmark",
            "bookmark": {"url": url}
        }

    def _parse_toggle(self, details_html: str, assets_dir: Path) -> Optional[Dict[str, Any]]:
        """Parse HTML details/summary into Notion toggle block."""
        # Extract summary text
        summary_match = re.search(r'<summary>(.*?)</summary>', details_html, re.DOTALL)
        if not summary_match:
            return None
        
        summary_text = summary_match.group(1).strip()
        
        # Extract content after </summary> and before </details>
        content_match = re.search(r'</summary>(.*)</details>', details_html, re.DOTALL)
        content_text = content_match.group(1).strip() if content_match else ""
        
        # Parse the content into child blocks
        child_blocks = []
        if content_text:
            child_blocks = self._parse_markdown(content_text, assets_dir)
        
        return {
            "type": "toggle",
            "toggle": {
                "rich_text": self._rich_text(summary_text),
                "children": child_blocks
            }
        }

    def _parse_table_columns(self, table_html: str, assets_dir: Path) -> Optional[Dict[str, Any]]:
        """Parse HTML table into Notion column_list block."""
        # Extract content from each <td>...</td>
        td_pattern = re.compile(r'<td[^>]*>(.*?)</td>', re.DOTALL)
        cells = td_pattern.findall(table_html)
        
        if not cells:
            return None
        
        columns = []
        for cell_content in cells:
            # Parse the cell content as markdown
            cell_content = cell_content.strip()
            # Remove surrounding newlines
            cell_content = re.sub(r'^\n+|\n+$', '', cell_content)
            
            # Parse cell content into blocks
            cell_blocks = self._parse_markdown(cell_content, assets_dir)
            
            if cell_blocks:
                column = {
                    "type": "column",
                    "column": {"children": cell_blocks}
                }
                columns.append(column)
        
        if columns:
            return {
                "type": "column_list",
                "column_list": {"children": columns}
            }
        return None

    # --- File Upload ---

    def _upload_file(self, file_path: Path) -> Optional[str]:
        """
        Upload a file and return its URL.
        Uses Cloudflare R2 if configured, otherwise returns None.
        """
        if self.file_uploader:
            return self.file_uploader.upload(file_path)
        
        print(f"[skip] No uploader configured, skipping: {file_path.name}")
        return None

    # --- Page Creation ---

    def create_page(self, title: str, parent_id: str, content_blocks: List[Dict[str, Any]], cover_url: str = None) -> str:
        """Create a new Notion page with content."""
        self._rate_limit()
        
        # Notion API limits to 100 blocks per request
        initial_blocks = content_blocks[:100]
        remaining_blocks = content_blocks[100:]
        
        # Build page creation params
        page_params = {
            "parent": {"page_id": parent_id},
            "properties": {"title": [{"text": {"content": title}}]},
            "children": initial_blocks,
        }
        
        # Add cover image if provided
        if cover_url and cover_url.startswith("http"):
            page_params["cover"] = {
                "type": "external",
                "external": {"url": cover_url}
            }
        
        page = self.client.pages.create(**page_params)
        
        page_id = page["id"]
        
        # Append remaining blocks in chunks
        while remaining_blocks:
            self._rate_limit()
            chunk = remaining_blocks[:100]
            remaining_blocks = remaining_blocks[100:]
            self.client.blocks.children.append(block_id=page_id, children=chunk)
        
        return page_id

    # --- Import Logic ---

    def import_directory(self, input_dir: Path) -> None:
        """Import all pages from the export directory."""
        input_dir = Path(input_dir)
        
        # Find all index.md files and their directories
        pages = []
        for md_file in input_dir.rglob("index.md"):
            rel_path = md_file.parent.relative_to(input_dir)
            depth = len(rel_path.parts)
            pages.append((depth, rel_path, md_file))
        
        # Sort by depth to create parent pages first
        pages.sort(key=lambda x: x[0])
        
        print(f"Found {len(pages)} pages to import")
        
        for depth, rel_path, md_file in pages:
            page_dir = md_file.parent
            assets_dir = page_dir / "assets"
            
            # Read markdown
            with open(md_file, "r", encoding="utf-8") as f:
                content = f.read()
            
            # Extract cover image if present (![cover](url) at the start)
            cover_url = None
            cover_match = re.match(r'^!\[cover\]\(([^)]+)\)\s*\n*', content)
            if cover_match:
                cover_url = cover_match.group(1)
                content = content[cover_match.end():]
            
            # Extract title from first heading or directory name
            title_match = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
            if title_match:
                title = title_match.group(1).strip()
                # Remove the title from content (Notion page already has title)
                content = re.sub(r"^#\s+.+\n+", "", content, count=1)
            else:
                title = page_dir.name.replace("-", " ").title()
            
            # Determine parent
            if depth == 0 or str(rel_path) == ".":
                parent_id = self.parent_page_id
            else:
                parent_path = rel_path.parent
                parent_id = self.created_pages.get(str(parent_path), self.parent_page_id)
            
            # Parse markdown to blocks
            blocks = self._parse_markdown(content, assets_dir)
            
            # Create page
            print(f"Creating: {title}")
            try:
                page_id = self.create_page(title, parent_id, blocks, cover_url=cover_url)
                self.created_pages[str(rel_path)] = page_id
                print(f"  ✓ Created: {title}")
            except Exception as e:
                print(f"  ✗ Failed: {title} - {e}")

    def import_single_page(self, md_file: Path) -> str:
        """Import a single markdown file."""
        md_file = Path(md_file)
        assets_dir = md_file.parent / "assets"
        
        with open(md_file, "r", encoding="utf-8") as f:
            content = f.read()
        
        # Extract cover image if present
        cover_url = None
        cover_match = re.match(r'^!\[cover\]\(([^)]+)\)\s*\n*', content)
        if cover_match:
            cover_url = cover_match.group(1)
            content = content[cover_match.end():]
        
        title_match = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()
            content = re.sub(r"^#\s+.+\n+", "", content, count=1)
        else:
            title = md_file.stem.replace("-", " ").title()
        
        blocks = self._parse_markdown(content, assets_dir)
        return self.create_page(title, self.parent_page_id, blocks, cover_url=cover_url)


def main():
    parser = argparse.ArgumentParser(
        description="Import markdown export into Notion"
    )
    parser.add_argument(
        "--token",
        required=True,
        help="Notion integration token (from https://www.notion.so/my-integrations)",
    )
    parser.add_argument(
        "--parent",
        required=True,
        help="Parent page ID where pages will be created",
    )
    parser.add_argument(
        "--input",
        default="export",
        help="Input directory containing exported markdown (default: export)",
    )
    
    # Cloudflare R2 options
    parser.add_argument(
        "--r2-account-id",
        help="Cloudflare Account ID",
    )
    parser.add_argument(
        "--r2-access-key",
        help="Cloudflare R2 Access Key ID",
    )
    parser.add_argument(
        "--r2-secret-key",
        help="Cloudflare R2 Secret Access Key",
    )
    parser.add_argument(
        "--r2-bucket",
        help="Cloudflare R2 bucket name",
    )
    parser.add_argument(
        "--r2-public-url",
        help="Custom public URL for R2 bucket (e.g., https://assets.yourdomain.com)",
    )
    
    args = parser.parse_args()

    importer = NotionImporter(args.token, args.parent)
    
    # Configure R2 uploader if credentials provided
    if args.r2_account_id and args.r2_access_key and args.r2_secret_key and args.r2_bucket:
        if not HAS_BOTO3:
            print("Error: boto3 is required for R2 uploads. Install with: pip install boto3")
            exit(1)
        
        if not args.r2_public_url:
            print("Error: --r2-public-url is required for R2 uploads.")
            print("       Find it in Cloudflare Dashboard → R2 → your bucket → Settings → Public R2.dev Bucket URL")
            exit(1)
        
        print("Configuring Cloudflare R2 for image uploads...")
        uploader = CloudflareR2Uploader(
            account_id=args.r2_account_id,
            access_key=args.r2_access_key,
            secret_key=args.r2_secret_key,
            bucket=args.r2_bucket,
            public_url=args.r2_public_url,
        )
        importer.set_r2_uploader(uploader)
        print(f"  ✓ R2 configured: {uploader.public_url_base}")
    else:
        print("Note: No R2 credentials provided. Images will be skipped.")
        print("      Use --r2-account-id, --r2-access-key, --r2-secret-key, --r2-bucket for image uploads.")
    
    print()
    importer.import_directory(args.input)
    print("\nImport complete!")


if __name__ == "__main__":
    main()
