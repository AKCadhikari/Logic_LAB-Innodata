# DocuSync AI screenshot guide

The report includes all nine screenshots from the running application. Each screenshot uses synthetic test documents. The cover uses the existing application icon. The architecture diagram is an illustration of the inspected code paths.

## Capture replacements

1. Start DocuSync AI locally with the current checkout. Open a browser at a desktop size near 1440 × 900 and leave the page zoom at 100%.
2. Prepare a synthetic PDF with a title, several positioned paragraphs and optional embedded metadata. Prepare a two-slide PPTX containing a clear spelling mistake. Use sample text you can share in the submission.
3. Capture the relevant screen at the steps below. Wait for page or slide images to render before capturing them. Use a clean browser profile or a separate workspace so Processing History contains only demonstration jobs.
4. Save each PNG in `report/screenshots/` under the given name. In the DOCX, replace the picture for the given figure, retain its caption, then export a fresh PDF and check the page count.

| Filename | Report placement | What to capture |
| --- | --- | --- |
| `01-home-dashboard.png` | Page 9, Fig. 7 | Open Home with both upload boxes visible before selecting files. |
| `02-pdf-upload.png` | Page 9, Fig. 8 | Select only the synthetic PDF. Capture the filename and the prompt to add a PowerPoint. |
| `03-pdf-zoning-layout.png` | Page 6, Fig. 2 | Process the PDF. Open **Zoning**, enable bounding boxes, and select a detection with its class and confidence visible. Wait for the underlying page image. |
| `04-reading-order.png` | Page 7, Fig. 3 | Open **Reading Order**. Select a numbered region so the page and the ordered block list are visible. |
| `05-metadata-inspector.png` | Page 7, Fig. 4 | Open **Source PDF** and **Metadata**. Show the page and the actual title, language or embedded metadata fields. |
| `06-xml-preview.png` | Page 8, Fig. 5 | Open **XML** after a successful export. Show the populated preview and **Download XML**. |
| `07-pptx-proofreading.png` | Page 8, Fig. 6 | Switch to **PowerPoint** after proofreading. Show the original slide preview, the completed result, and the expanded list of corrections. |
| `08-processing-progress.png` | Page 9, Fig. 9 | Capture the genuine queued or processing status while the original PDF is still visible. Take this immediately after selecting **Start Processing**. |
| `09-generated-output.png` | Page 9, Fig. 10 | Open **Processing History** after the synthetic job completes. Show the completed row and its XML download action. |

Check the captures for client content, private filenames, user identifiers, local paths and credentials before distributing them. Use actual application states when replacing the images. If an image is unavailable, retain a clearly labeled placeholder with its planned filename and caption.
