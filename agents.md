# agents.md

## Project: Notion Public Page Exporter & Importer

Tools to export public Notion pages to Markdown and re-import them via the official Notion API.

---

## Scripts

### `notion_public_export.py`
Exports public Notion pages (and subpages) to Markdown + local assets using Notion's internal `api/v3` endpoints.

**Features:**
- Resolves page IDs from any Notion URL (including custom domains)
- Downloads images, PDFs, videos (signs S3 URLs via Notion's internal API)
- Handles collections/databases and recursively exports subpages
- Exports rich text: bold, italic, strikethrough, code, underline, links
- Exports colors/backgrounds as HTML `<span style="...">`
- Exports columns as HTML tables
- Exports callouts (with emoji icons), toggles (`<details>`), embeds (YouTube/Vimeo as links)
- Exports page cover images as `![cover](url)` at the top
- Resolves page mentions to actual page titles with links
- Resolves link mentions (bookmarks) to titled links

**Usage:**
```bash
# Basic export
python3 notion_public_export.py "https://notion.so/page-url" -o export

# With authentication (for some pages)
NOTION_TOKEN="token_v2" python3 notion_public_export.py "https://notion.so/page-url" -o export

# Skip specific pages by title
python3 notion_public_export.py "https://notion.so/page-url" -o export --skip "Page Title To Skip"
```

**Requirements:**
- Python 3.10+
- `requests`

---

### `notion_import.py`
Imports exported Markdown back into Notion via the official API.

**Features:**
- Parses Markdown with full formatting: bold, italic, strikethrough, code, underline
- Preserves text colors (purple, blue, etc.) and background colors via Notion annotations
- Converts callouts (blockquotes with emoji) to Notion callout blocks
- Converts `<details><summary>` to Notion toggle blocks
- Converts HTML tables to Notion column layouts
- Sets page cover images from `![cover](url)`
- Supports image uploads via Cloudflare R2
- Creates hierarchical page structure

**Usage:**
```bash
# Basic import (without images)
python3 notion_import.py \
  --token "your-notion-integration-token" \
  --parent "https://www.notion.so/your-page-url" \
  --input export/

# With Cloudflare R2 for images
python3 notion_import.py \
  --token "your-notion-integration-token" \
  --parent "https://www.notion.so/your-page-url" \
  --input export/ \
  --r2-account-id "account-id" \
  --r2-access-key "access-key" \
  --r2-secret-key "secret-key" \
  --r2-bucket "bucket-name" \
  --r2-public-url "https://your-bucket.r2.dev"
```

**Setup:**
1. Create an integration at https://www.notion.so/my-integrations
2. Copy the "Internal Integration Token"
3. Share the target Notion page with your integration (click ... → Connections → Add your integration)

**Requirements:**
- Python 3.10+
- `notion-client` (`pip install notion-client`)
- `boto3` (optional, for R2 uploads: `pip install boto3`)

---

## Formatting Preservation

The export/import cycle preserves:
- **Text formatting**: bold, italic, bold+italic, strikethrough, code, underline
- **Colors**: Text colors and background highlights (mapped to Notion's color palette)
- **Links**: External URLs and internal page references
- **Block types**: Headers (h1-h3), bullet/numbered lists, quotes, callouts, toggles, dividers, code blocks
- **Layout**: Column layouts (via HTML tables)
- **Media**: Images (with R2), videos/audio as embed links
- **Cover images**: Page covers exported and re-imported

---

## Known Limitations

### Export
- `attachment:` URLs require the internal API to resolve to signed S3 URLs
- Database/collection views are exported as flat pages, not as databases
- Inline equations not supported (rare in most pages)
- Page navigation structure not reconstructed

### Import
- Base64 data URLs for images are skipped (Notion API doesn't support them)
- Nested list indentation may not be perfectly preserved
- Some complex block types may fall back to paragraphs
- Images require external hosting (Cloudflare R2 or similar)

---

## Color Mapping

| CSS Color | Notion Color |
|-----------|--------------|
| #787774 | gray |
| #9F6B53 | brown |
| #D9730D | orange |
| #CB912F | yellow |
| #448361 | green |
| #337EA9 | blue |
| #9065B0 | purple |
| #C14C8A | pink |
| #D44C47 | red |
| + background variants |

---

## File Structure

```
notion-public-export/
├── agents.md                 # This file
├── notion_public_export.py   # Export script
├── notion_import.py          # Import script
└── export/                   # Default export directory
    ├── page-slug/
    │   ├── index.md
    │   └── assets/
    │       └── image.png
    └── ...
```

---

## Development Notes

- Notion's internal API (`api/v3`) is undocumented and may change
- Rate limiting: 0.3s delay between API calls by default
- The exporter caches page titles to avoid redundant API calls
- Toggle blocks and columns use HTML in the Markdown for round-trip fidelity
