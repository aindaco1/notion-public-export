#!/usr/bin/env python3
"""
Export a public Notion page (and its subpages) to Markdown + local assets,
using Notion's internal /api/v3 endpoints.

⚠️ This uses private, undocumented APIs and may break if Notion changes them.
Use for personal archiving, not production code.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse

import requests


API_BASE = "https://www.notion.so/api/v3"


class NotionPublicExporter:
    def __init__(self, output_dir: str, token: Optional[str] = None, delay: float = 0.3):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.session = requests.Session()
        self.session.headers.update({
            "Content-Type": "application/json",
            "User-Agent": "notion-public-exporter/0.1",
        })
        # Optional: cookie auth (works for some public pages that still expect token_v2)
        if token:
            self.session.cookies.set("token_v2", token)

        self.delay = delay
        self.seen_pages = set()
        self._page_title_cache: Dict[str, str] = {}
        self.skip_titles = set()  # Page titles to skip during export

    # --- Helpers ---------------------------------------------------------

    def _normalize_page_id(self, url_or_id: str) -> str:
        """
        Extract and normalize a 32-char page ID from a Notion URL, custom domain
        URL, or raw ID. Normalized form: 8-4-4-4-12 UUID.

        If no 32-char ID is found in the string itself and it looks like a URL,
        we fetch the HTML and try to find a 32-char ID in the page source.
        """
        # First, try to find a 32-char hex-ish string directly in the input
        m = re.search(r'([0-9a-fA-F]{32})', url_or_id)
        # Also try hyphenated UUID format (8-4-4-4-12)
        m_uuid = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', url_or_id)
        
        if not m and not m_uuid and url_or_id.startswith(("http://", "https://")):
            # Fallback: fetch HTML and search for an ID there
            from requests.exceptions import RequestException

            try:
                resp = self.session.get(url_or_id)
                resp.raise_for_status()
            except RequestException as e:
                raise ValueError(
                    f"Could not resolve page id from URL (HTTP error): {url_or_id}"
                ) from e

            html = resp.text or ""
            # First try to find pageId in requiredRedirectMetadata JSON
            m_page_id = re.search(r'"pageId"\s*:\s*"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"', html)
            if m_page_id:
                m_uuid = m_page_id
            else:
                # Fallback: try hyphenated UUID, then 32-char hex
                m_uuid = re.search(r'([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})', html)
            if not m_uuid:
                m = re.search(r'([0-9a-fA-F]{32})', html)

        if not m and not m_uuid:
            raise ValueError(f"Could not find 32-character page id in '{url_or_id}'")

        # If we found a hyphenated UUID, return it directly (already normalized)
        if m_uuid:
            return m_uuid.group(1).lower()
        
        raw = m.group(1).lower()
        return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"

    def _api_post(self, endpoint: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{API_BASE}/{endpoint}"
        resp = self.session.post(url, data=json.dumps(payload))
        if resp.status_code != 200:
            raise RuntimeError(
                f"Notion API error {resp.status_code} for {endpoint}: {resp.text[:200]}"
            )
        time.sleep(self.delay)
        return resp.json()

    def _get_page_title(self, page_id: str) -> Optional[str]:
        """
        Fetch the title of a page by its ID. Uses a cache to avoid repeated API calls.
        """
        if page_id in self._page_title_cache:
            return self._page_title_cache[page_id]

        try:
            # Normalize the page ID
            normalized_id = self._normalize_page_id(page_id)
            record_map = self.fetch_record_map(normalized_id)
            blocks = record_map.get("block", {}) or {}
            page_block = blocks.get(normalized_id)
            if page_block:
                bval = page_block.get("value", page_block)
                title_prop = (bval.get("properties") or {}).get("title", [])
                title = self._rich_text_to_plain(title_prop)
                if title:
                    self._page_title_cache[page_id] = title
                    return title
        except (ValueError, RuntimeError):
            pass

        return None

    # --- Core fetch logic ------------------------------------------------

    def query_collection(self, collection_id: str, collection_view_id: str) -> list:
        """
        Query a collection (database) to get all page IDs it contains.
        """
        payload = {
            "collection": {"id": collection_id},
            "collectionView": {"id": collection_view_id},
            "loader": {
                "type": "reducer",
                "reducers": {
                    "collection_group_results": {
                        "type": "results",
                        "limit": 999,
                    }
                },
                "searchQuery": "",
                "userTimeZone": "America/New_York",
            },
        }

        try:
            data = self._api_post("queryCollection", payload)
        except RuntimeError:
            return []

        result = data.get("result", {})
        reducers = result.get("reducerResults", {})
        group_results = reducers.get("collection_group_results", {})
        block_ids = group_results.get("blockIds", [])
        return block_ids

    def fetch_record_map(self, page_id: str) -> Dict[str, Any]:
        """
        Fetch the full recordMap for a page by paging through loadPageChunk.
        Falls back to loadCachedPageChunk if needed.
        """
        cursor = {"stack": []}
        chunk_number = 0
        merged_blocks: Dict[str, Any] = {}

        while True:
            payload = {
                "page": {"id": page_id},
                "limit": 100,
                "cursor": cursor,
                "chunkNumber": chunk_number,
                "verticalColumns": False,
            }

            try:
                data = self._api_post("loadPageChunk", payload)
            except RuntimeError:
                # Some stacks use loadCachedPageChunk instead
                data = self._api_post("loadCachedPageChunk", payload)

            record_map = data.get("recordMap", {})
            blocks = record_map.get("block", {}) or {}
            if not blocks:
                break

            merged_blocks.update(blocks)

            new_cursor = data.get("cursor", {})
            # If cursor doesn't change or stack is empty, stop
            if not new_cursor or new_cursor == cursor or not new_cursor.get("stack"):
                break

            cursor = new_cursor
            chunk_number += 1

        return {"block": merged_blocks}

    # --- Markdown rendering ----------------------------------------------

    @staticmethod
    def _slugify(text: str) -> str:
        slug = re.sub(r"[^\w\-]+", "-", text.lower()).strip("-")
        return slug or "page"

    # Map Notion color names to CSS colors
    COLOR_MAP = {
        "gray": "#787774",
        "brown": "#9F6B53",
        "orange": "#D9730D",
        "yellow": "#CB912F",
        "green": "#448361",
        "blue": "#337EA9",
        "purple": "#9065B0",
        "pink": "#C14C8A",
        "red": "#D44C47",
        "gray_background": "#F1F1EF",
        "brown_background": "#F4EEEE",
        "orange_background": "#FBECDD",
        "yellow_background": "#FBF3DB",
        "green_background": "#EDF3EC",
        "blue_background": "#E7F3F8",
        "purple_background": "#F6F3F9",
        "pink_background": "#FAF1F5",
        "red_background": "#FDEBEC",
    }

    def _rich_text_to_markdown(self, rich: Any, use_html_colors: bool = True) -> str:
        """
        Convert Notion's rich text format to Markdown with formatting.
        
        Notion format: [["Hello", [["b"]]], [" world", [["i"]]], ["link", [["a", "url"]]]]
        Annotations: b=bold, i=italic, s=strikethrough, c=code, a=link, _=underline, h=highlight/color
        """
        if not rich:
            return ""
        
        parts = []
        for part in rich:
            if not isinstance(part, list) or not part:
                continue
            
            text = str(part[0])
            annotations = part[1] if len(part) > 1 and part[1] else []
            
            # Process annotations
            is_bold = False
            is_italic = False
            is_strikethrough = False
            is_code = False
            is_underline = False
            link_url = None
            highlight_color = None
            
            for ann in annotations:
                if isinstance(ann, list) and ann:
                    ann_type = ann[0]
                    if ann_type == "b":
                        is_bold = True
                    elif ann_type == "i":
                        is_italic = True
                    elif ann_type == "s":
                        is_strikethrough = True
                    elif ann_type == "c":
                        is_code = True
                    elif ann_type == "_":
                        is_underline = True
                    elif ann_type == "a" and len(ann) > 1:
                        link_url = ann[1]
                    elif ann_type == "p" and len(ann) > 1:
                        # Page mention - the format is ["p", "page_id", "space_id"]
                        page_mention_id = ann[1]
                        link_url = f"/{page_mention_id.replace('-', '')}"
                        # Check cache first, fetch if not cached
                        if page_mention_id in self._page_title_cache:
                            text = self._page_title_cache[page_mention_id]
                        else:
                            # Fetch title for uncached page mentions
                            fetched_title = self._get_page_title(page_mention_id)
                            if fetched_title:
                                text = fetched_title
                    elif ann_type == "lm" and len(ann) > 1:
                        # Link mention (bookmark) - format is ["lm", {href, title, description, ...}]
                        link_meta = ann[1]
                        if isinstance(link_meta, dict):
                            link_url = link_meta.get("href")
                            link_title = link_meta.get("title")
                            if link_title:
                                text = link_title
                    elif ann_type == "h" and len(ann) > 1:
                        color = ann[1]
                        if color and color != "default":
                            highlight_color = color
            
            # Apply formatting (order matters for nesting)
            # Handle trailing/leading whitespace - markdown formatting fails with spaces inside markers
            leading_space = ""
            trailing_space = ""
            if text and text[0] == " ":
                leading_space = " "
                text = text[1:]
            if text and text[-1:] == " ":
                trailing_space = " "
                text = text[:-1]
            
            # Only apply formatting if there's actual text content
            if text:
                if is_code:
                    text = f"`{text}`"
                if is_strikethrough:
                    text = f"~~{text}~~"
                if is_underline:
                    text = f"<u>{text}</u>"
                if is_bold and is_italic:
                    text = f"***{text}***"
                elif is_bold:
                    text = f"**{text}**"
                elif is_italic:
                    text = f"*{text}*"
                if link_url:
                    text = f"[{text}]({link_url})"
            
            # Restore whitespace outside the formatting
            text = leading_space + text + trailing_space
            
            # Apply color/highlight using HTML span
            if highlight_color and use_html_colors:
                css_color = NotionPublicExporter.COLOR_MAP.get(highlight_color)
                if css_color:
                    if "_background" in highlight_color:
                        text = f'<span style="background-color: {css_color}">{text}</span>'
                    else:
                        text = f'<span style="color: {css_color}">{text}</span>'
            
            parts.append(text)
        
        return "".join(parts)
    
    @staticmethod
    def _rich_text_to_plain(rich: Any) -> str:
        """
        Extract plain text from Notion's rich text format (no formatting).
        """
        if not rich:
            return ""
        parts = []
        for part in rich:
            if isinstance(part, list) and part:
                parts.append(str(part[0]))
        return "".join(parts)

    def _resolve_attachment_url(self, url: str, block_id: str, space_id: str) -> Optional[str]:
        """
        Resolve an attachment:file_id:filename URL to a signed S3 URL.
        """
        if not url.startswith("attachment:"):
            return None

        parts = url.split(":", 2)
        if len(parts) < 3:
            return None

        file_id = parts[1]
        filename = parts[2]

        # Construct the S3 URL
        s3_url = f"https://prod-files-secure.s3.us-west-2.amazonaws.com/{space_id}/{file_id}/{filename}"

        # Get signed URL from Notion API
        payload = {
            "urls": [{
                "url": s3_url,
                "permissionRecord": {"table": "block", "id": block_id}
            }]
        }

        try:
            data = self._api_post("getSignedFileUrls", payload)
            signed_urls = data.get("signedUrls", [])
            if signed_urls and signed_urls[0]:
                return signed_urls[0]
        except RuntimeError:
            pass

        return None

    def _sign_s3_url(self, url: str, block_id: str) -> Optional[str]:
        """
        Sign a prod-files-secure S3 URL using Notion's getSignedFileUrls API.
        """
        payload = {
            "urls": [{
                "url": url,
                "permissionRecord": {"table": "block", "id": block_id}
            }]
        }

        try:
            data = self._api_post("getSignedFileUrls", payload)
            signed_urls = data.get("signedUrls", [])
            if signed_urls and signed_urls[0]:
                return signed_urls[0]
        except RuntimeError:
            pass

        return None

    def _download_file(self, url: str, page_dir: Path, block_id: str = None, space_id: str = None) -> str:
        """
        Download a file/image to page_dir/assets and return a relative path.

        If the URL is not an HTTP(S) URL (e.g. 'attachment:...'), we try to
        resolve it to a signed S3 URL first.
        """
        import hashlib
        from requests.exceptions import RequestException

        if not url:
            return ""

        # Try to resolve attachment: URLs to signed S3 URLs
        if url.startswith("attachment:") and block_id and space_id:
            signed_url = self._resolve_attachment_url(url, block_id, space_id)
            if signed_url:
                url = signed_url

        # Sign prod-files-secure S3 URLs (they return 403 without signature)
        if "prod-files-secure.s3" in url and block_id:
            signed_url = self._sign_s3_url(url, block_id)
            if signed_url:
                url = signed_url

        # Skip non-http(s) schemes if we couldn't resolve them
        if not (url.startswith("http://") or url.startswith("https://")):
            return url

        assets_dir = page_dir / "assets"
        assets_dir.mkdir(exist_ok=True)

        parsed = urlparse(url)
        base_name = os.path.basename(parsed.path)
        if not base_name:
            base_name = "file"
        
        # Add URL hash to filename to avoid collisions when multiple files have same name
        url_hash = hashlib.md5(url.encode("utf-8")).hexdigest()[:8]
        name_parts = os.path.splitext(base_name)
        name = f"{name_parts[0]}_{url_hash}{name_parts[1]}"

        out_path = assets_dir / name
        if out_path.exists():
            return os.path.join("assets", name)

        try:
            r = self.session.get(url, stream=True)
            if r.status_code == 200:
                with out_path.open("wb") as f:
                    for chunk in r.iter_content(8192):
                        if chunk:
                            f.write(chunk)
                return os.path.join("assets", name)
            else:
                # Non-200 – keep markdown link pointing at the original URL
                return url
        except RequestException:
            # Network/schema/other requests-related issue – don't crash
            return url

    def _render_block(
        self,
        block_id: str,
        blocks: Dict[str, Any],
        out,
        indent: int,
        page_dir: Path,
        space_id: str = None,
    ) -> None:
        block_wrapper = blocks.get(block_id)
        if not block_wrapper:
            return
        block = block_wrapper.get("value") if "value" in block_wrapper else block_wrapper
        if not block or block.get("alive") is False:
            return

        btype = block.get("type")
        props = block.get("properties", {}) or {}
        title_prop = props.get("title", [])
        # Use markdown formatting for rich text
        text = self._rich_text_to_markdown(title_prop)
        plain_text = self._rich_text_to_plain(title_prop)
        
        # Get space_id from block if not provided
        if not space_id:
            space_id = block.get("space_id")

        # Column layout - render as a table or just sequential content
        if btype == "column_list":
            child_ids = block.get("content") or []
            if child_ids:
                # Collect content from each column
                columns_content = []
                for col_id in child_ids:
                    col_wrapper = blocks.get(col_id)
                    if col_wrapper:
                        col_block = col_wrapper.get("value", col_wrapper)
                        if col_block and col_block.get("type") == "column":
                            col_children = col_block.get("content") or []
                            columns_content.append(col_children)
                
                # Render columns side by side using HTML table (works in many markdown renderers)
                if len(columns_content) > 1:
                    out.write('\n<table><tr>\n')
                    for col_children in columns_content:
                        out.write('<td valign="top">\n\n')
                        for cid in col_children:
                            self._render_block(cid, blocks, out, 0, page_dir, space_id=space_id)
                        out.write('\n</td>\n')
                    out.write('</tr></table>\n\n')
                else:
                    # Single column, just render normally
                    for col_children in columns_content:
                        for cid in col_children:
                            self._render_block(cid, blocks, out, indent, page_dir, space_id=space_id)
            return  # Don't process children again below
        
        elif btype == "column":
            # Columns are handled by column_list above
            return

        # Basic block types
        elif btype == "header":
            out.write(f"\n# {text}\n\n")
        elif btype == "sub_header":
            out.write(f"\n## {text}\n\n")
        elif btype == "sub_sub_header":
            out.write(f"\n### {text}\n\n")
        elif btype == "bulleted_list":
            out.write("  " * indent + f"- {text}\n")
        elif btype == "numbered_list":
            out.write("  " * indent + f"1. {text}\n")
        elif btype == "to_do":
            checked_prop = props.get("checked") or [["No"]]
            checked = self._rich_text_to_plain(checked_prop) in ("Yes", "true", "True")
            mark = "x" if checked else " "
            out.write("  " * indent + f"- [{mark}] {text}\n")
        elif btype == "quote":
            out.write(f"> {text}\n\n")
        elif btype == "callout":
            # Callouts - render with emoji/icon if available
            fmt = block.get("format") or {}
            icon = fmt.get("page_icon", "💡")
            # Handle Notion's icon paths
            if icon.startswith("/icons/"):
                icon = "➡️"  # Default fallback for Notion's built-in icons
            
            # Get child content if title is empty (Notion sometimes puts text in first child)
            child_ids = block.get("content") or []
            if not text.strip() and child_ids:
                # Use only the first child's text for the callout
                first_child_wrapper = blocks.get(child_ids[0])
                if first_child_wrapper:
                    first_child = first_child_wrapper.get("value", first_child_wrapper)
                    if first_child:
                        child_props = first_child.get("properties", {}) or {}
                        child_title = child_props.get("title", [])
                        text = self._rich_text_to_markdown(child_title)
                        # Remove the first child since we used it for the callout text
                        child_ids = child_ids[1:]
            
            out.write(f"> {icon} {text}\n\n")
            # Render any remaining children normally
            for cid in child_ids:
                self._render_block(cid, blocks, out, indent, page_dir, space_id=space_id)
            return  # Don't process children again below
        elif btype == "code":
            lang_prop = props.get("language") or [[""]]
            lang = self._rich_text_to_plain(lang_prop)
            # Use plain text for code blocks (no markdown formatting inside)
            out.write(f"\n```{lang}\n{plain_text}\n```\n\n")
        elif btype == "toggle":
            # Toggle blocks - render as details/summary HTML
            out.write(f"\n<details>\n<summary>{text}</summary>\n\n")
            child_ids = block.get("content") or []
            for cid in child_ids:
                self._render_block(cid, blocks, out, indent, page_dir, space_id=space_id)
            out.write("\n</details>\n\n")
            return  # Don't process children again below
        elif btype == "embed":
            # External embeds (Google Forms, YouTube, etc.) - render as link, don't download
            fmt = block.get("format") or {}
            url = fmt.get("display_source") or fmt.get("source")
            if not url:
                src_prop = props.get("source") or [[""]]
                url = self._rich_text_to_plain(src_prop)
            if url:
                label = text or fmt.get("link_title") or "Embedded content"
                out.write(f"[{label}]({url})\n\n")
        elif btype == "video":
            # Check if it's an external video platform or a downloadable file
            fmt = block.get("format") or {}
            src_prop = props.get("source") or [[""]]
            url = self._rich_text_to_plain(src_prop)
            if not url:
                url = fmt.get("display_source") or fmt.get("source")
            
            # External video platforms - render as link
            external_platforms = ("youtube.com", "youtu.be", "vimeo.com", "wistia.com", 
                                  "dailymotion.com", "twitch.tv", "facebook.com/watch")
            if url and any(platform in url.lower() for platform in external_platforms):
                label = fmt.get("link_title") or text or "Video"
                # Use the watch URL, not the embed URL
                watch_url = self._rich_text_to_plain(src_prop) or url
                out.write(f"[▶ {label}]({watch_url})\n\n")
            elif url:
                # Downloadable video file (mp4, mov, webm, etc.)
                rel = self._download_file(url, page_dir, block_id=block_id, space_id=space_id)
                label = text or "video"
                out.write(f"![{label}]({rel})\n\n")
        elif btype == "audio":
            # Check if it's an external audio platform or a downloadable file
            fmt = block.get("format") or {}
            src_prop = props.get("source") or [[""]]
            url = self._rich_text_to_plain(src_prop)
            if not url:
                url = fmt.get("display_source") or fmt.get("source")
            
            # External audio platforms - render as link
            external_platforms = ("soundcloud.com", "spotify.com", "open.spotify.com", 
                                  "podcasts.apple.com", "anchor.fm")
            if url and any(platform in url.lower() for platform in external_platforms):
                label = fmt.get("link_title") or text or "Audio"
                out.write(f"[🔊 {label}]({url})\n\n")
            elif url:
                # Downloadable audio file (mp3, wav, etc.)
                rel = self._download_file(url, page_dir, block_id=block_id, space_id=space_id)
                label = text or "audio"
                out.write(f"![{label}]({rel})\n\n")
        elif btype in ("image", "file", "pdf"):
            # Try properties.source first
            src_prop = props.get("source") or [[""]]
            url = self._rich_text_to_plain(src_prop)

            # Fallback to format.display_source / format.source
            if not url:
                fmt = block.get("format") or {}
                url = fmt.get("display_source") or fmt.get("source")

            if url:
                rel = self._download_file(url, page_dir, block_id=block_id, space_id=space_id)
                label = text or btype
                out.write(f"![{label}]({rel})\n\n")
        elif btype == "link_to_page":
            # Internal link to another page
            fmt = block.get("format") or {}
            target_id = fmt.get("page_id") or fmt.get("page_ref")
            label = text or "linked page"
            if target_id:
                slug = self._slugify(label or target_id[:6])
                out.write(f"\n[{label}]({slug}/index.md)\n\n")
        elif btype == "divider":
            out.write("\n---\n\n")
        elif btype == "child_page":
            # A link to the subpage (we'll export it separately)
            slug = self._slugify(text or block.get("id", "")[:6])
            out.write(f"\n[{text or slug}]({slug}/index.md)\n\n")
        elif btype == "text":
            # Text blocks - add blank line before and after for proper paragraph separation
            if text:
                out.write(f"\n{text}\n")
        else:
            if text:
                out.write(text + "\n")

        # Render children (nested content)
        child_ids = block.get("content") or []
        next_indent = indent + (1 if btype in ("bulleted_list", "numbered_list", "to_do") else 0)
        for cid in child_ids:
            self._render_block(cid, blocks, out, next_indent, page_dir, space_id=space_id)

    # --- Export pipeline -------------------------------------------------

    def export_tree(self, root_url: str) -> None:
        root_id = self._normalize_page_id(root_url)
        self._export_page(root_id, parent_dir=self.output_dir)

    def _export_page(self, page_id: str, parent_dir: Path) -> None:
        if page_id in self.seen_pages:
            return
        self.seen_pages.add(page_id)

        record_map = self.fetch_record_map(page_id)
        blocks = record_map.get("block", {}) or {}

        # Find the page block
        page_block_wrapper = blocks.get(page_id)
        if not page_block_wrapper:
            print(f"[warn] No page block found for {page_id}")
            return

        page_block = page_block_wrapper.get("value", page_block_wrapper)
        title_prop = (page_block.get("properties") or {}).get("title", [])
        page_title = self._rich_text_to_plain(title_prop) or "untitled"
        
        # Skip pages with titles in the skip list
        if page_title in self.skip_titles:
            return
            
        page_slug = self._slugify(page_title)
        space_id = page_block.get("space_id")
        
        # Cache the title for page mentions
        self._page_title_cache[page_id] = page_title

        page_dir = parent_dir / page_slug
        page_dir.mkdir(parents=True, exist_ok=True)

        md_path = page_dir / "index.md"
        with md_path.open("w", encoding="utf-8") as f:
            # Add cover image if present
            fmt = page_block.get("format") or {}
            page_cover = fmt.get("page_cover")
            if page_cover:
                # Build the cover image URL
                if page_cover.startswith("attachment:"):
                    # Format: attachment:file_id:filename
                    cover_url = f"https://www.notion.so/image/{requests.utils.quote(page_cover, safe='')}?table=block&id={page_id}&spaceId={space_id}&width=2000&cache=v2"
                elif page_cover.startswith("http"):
                    cover_url = page_cover
                else:
                    cover_url = f"https://www.notion.so{page_cover}"
                f.write(f"![cover]({cover_url})\n\n")
            
            f.write(f"# {page_title}\n\n")
            for cid in page_block.get("content") or []:
                self._render_block(cid, blocks, f, indent=0, page_dir=page_dir, space_id=space_id)

        # Recurse into subpages:
        #  - child_page blocks
        #  - page blocks (newer style)
        #  - pages referenced via link_to_page
        for bid, bw in blocks.items():
            bval = bw.get("value", bw)
            if not bval or bval.get("alive") is False:
                continue

            btype = bval.get("type")
            if bval.get("id") == page_id:
                continue

            # Old-style and newer-style pages we see in this recordMap
            if btype in ("child_page", "page"):
                self._export_page(bval["id"], parent_dir=page_dir)

            # link_to_page blocks are just links to other pages, not children
            # Don't export them - they may point to entirely different page trees

            # collection_view blocks contain databases with pages
            # Export them flat under current page, not recursively
            if btype == "collection_view":
                view_ids = bval.get("view_ids") or []
                fmt = bval.get("format") or {}
                collection_pointer = fmt.get("collection_pointer", {})
                collection_id = collection_pointer.get("id")
                
                if collection_id and view_ids:
                    # Query the first view to get all pages
                    page_ids = self.query_collection(collection_id, view_ids[0])
                    for sub_page_id in page_ids:
                        # Export collection items at root level to avoid deep nesting
                        self._export_page(sub_page_id, parent_dir=self.output_dir)


def main():
    parser = argparse.ArgumentParser(
        description="Export a public Notion page (and its subpages) to Markdown."
    )
    parser.add_argument(
        "url",
        help="Public Notion page URL (e.g. https://thedistributionplaybook.notion.site/)",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="notion-export",
        help="Output directory (default: notion-export)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("NOTION_TOKEN"),
        help="Optional token_v2 cookie for Notion (or set NOTION_TOKEN env var).",
    )
    parser.add_argument(
        "--skip",
        action="append",
        default=[],
        help="Page titles to skip during export (can be used multiple times).",
    )
    args = parser.parse_args()

    exporter = NotionPublicExporter(args.output, token=args.token)
    exporter.skip_titles = set(args.skip)
    exporter.export_tree(args.url)
    print(f"Done. Exported to {args.output}")


if __name__ == "__main__":
    main()