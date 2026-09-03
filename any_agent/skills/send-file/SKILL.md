---
name: send-file
description: Deliver a local file (image, video, or document) to the user over WeChat. Use whenever the user asks you to send, share, or show them a file you created or found on disk.
---

# Sending a file to the user

The user talks to you through a WeChat bridge. Your normal text output reaches them,
but files on disk do **not** — unless you emit a `sendfile://` marker.

To deliver a file, print a line **on its own** in this exact form:

```
sendfile://<URL-ENCODED-ABSOLUTE-PATH>
```

The bridge scans your reply for these lines, uploads each file to WeChat, and sends it
to the user. The marker line itself is stripped from the text they see.

## Rules

1. **Absolute path only.** Start with `/`. Relative paths are ignored.
2. **URL-encode the path.** Spaces become `%20`, and other special characters are
   percent-encoded (e.g. `/tmp/my report.png` → `sendfile:///tmp/my%20report.png`).
   Note the three slashes: `sendfile://` + the leading `/` of the absolute path.
3. **One marker per line.** To send several files, print several lines.
4. **Optional caption** with `?caption=<url-encoded text>`:
   ```
   sendfile:///tmp/chart.png?caption=Here%20is%20your%20chart
   ```
5. **Actually send the file** — do not just describe it or paste the path in prose.
   You may add a short sentence of normal text before/after the marker.

## Examples

Send one image:
```
Here's the chart you asked for.
sendfile:///Users/me/output/chart.png
```

Send a document with a spaced filename and a caption:
```
sendfile:///Users/me/reports/Q3%20summary.pdf?caption=Q3%20financial%20summary
```

Send two files at once:
```
sendfile:///tmp/before.png
sendfile:///tmp/after.png
```

Supported types: images (png, jpg, gif, webp, bmp), video (mp4, mov, webm, mkv, avi),
and any other file is delivered as a file attachment (pdf, zip, csv, docx, ...).
