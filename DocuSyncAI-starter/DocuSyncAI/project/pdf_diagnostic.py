
import pymupdf
import json
import argparse
from pathlib import Path
from datetime import datetime


class PDFDiagnosticEngine:

    def __init__(self):
        self.results = []

    def analyse_page(self, page, page_number):

        text = page.get_text("text")
        words = page.get_text("words")
        images = page.get_images(full=True)

        text_chars = len(text.strip())
        word_count = len(words)

        page_area = page.rect.width * page.rect.height

        image_area = 0

        for image in images:
            image_xrefs = image[0]

            for rect in page.get_image_rects(image_xrefs):
                image_area += rect.width * rect.height

        image_coverage = (
            min(image_area / page_area, 1.0)
            if page_area > 0 else 0
        )

        suspicious_markers = [
            "\ufffd",
            "Ã",
            "Â",
            "â€",
            "\x00"
        ]

        suspicious_count = sum(
            text.count(marker)
            for marker in suspicious_markers
        )

        suspicious_ratio = (
            suspicious_count / max(text_chars, 1)
        )

        is_rotated = page.rotation != 0

        if text_chars < 30:
            recommendation = "OCR_REQUIRED"

        elif suspicious_ratio > 0.02:
            recommendation = "TEXT_QUALITY_REVIEW"

        elif image_coverage > 0.70:
            recommendation = "IMAGE_HEAVY_REVIEW"

        else:
            recommendation = "NATIVE_EXTRACTION"

        return {
            "page_number": page_number,
            "text_characters": text_chars,
            "word_count": word_count,
            "image_count": len(images),
            "image_coverage": round(image_coverage, 3),
            "rotation": page.rotation,
            "is_rotated": is_rotated,
            "suspicious_character_ratio": round(
                suspicious_ratio, 4
            ),
            "recommendation": recommendation
        }

    def analyse_pdf(self, pdf_path):

        pdf_path = Path(pdf_path)

        print(f"\nAnalysing: {pdf_path.name}")

        document = pymupdf.open(pdf_path)

        page_results = []

        for index, page in enumerate(document):

            result = self.analyse_page(
                page,
                index + 1
            )

            page_results.append(result)

            print(
                f"Page {index + 1}: "
                f"{result['recommendation']} | "
                f"Characters: {result['text_characters']}"
            )

        total_pages = len(document)

        result = {
            "filename": pdf_path.name,
            "total_pages": total_pages,
            "total_characters": sum(
                p["text_characters"] for p in page_results
            ),
            "ocr_required_pages": sum(
                p["recommendation"] == "OCR_REQUIRED"
                for p in page_results
            ),
            "review_pages": sum(
                p["recommendation"] in [
                    "TEXT_QUALITY_REVIEW",
                    "IMAGE_HEAVY_REVIEW"
                ]
                for p in page_results
            ),
            "pages": page_results
        }

        document.close()

        return result

    def analyse_directory(self, input_dir, output_dir):

        input_dir = Path(input_dir)
        output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)

        pdf_files = sorted(input_dir.glob("*.pdf"))

        if not pdf_files:
            print("No PDF files found in:", input_dir)
            return

        print("=" * 55)
        print("DOCUSYNC AI - PDF DIAGNOSTIC ENGINE")
        print("=" * 55)

        for pdf_file in pdf_files:

            try:
                result = self.analyse_pdf(pdf_file)
                self.results.append(result)

            except Exception as error:

                print(f"ERROR: {pdf_file.name}: {error}")

                self.results.append({
                    "filename": pdf_file.name,
                    "error": str(error)
                })

        report = {
            "system": "DocuSync AI",
            "engine": "PDFDiagnosticEngine",
            "generated_at": datetime.now().isoformat(),
            "total_documents": len(pdf_files),
            "successful_documents": sum(
                "error" not in item for item in self.results
            ),
            "documents": self.results
        }

        report_path = output_dir / "diagnostic_report.json"

        with open(report_path, "w", encoding="utf-8") as file:

            json.dump(
                report,
                file,
                indent=4,
                ensure_ascii=False
            )

        print("\n" + "=" * 55)
        print("DIAGNOSTIC COMPLETED")
        print("=" * 55)

        print("Documents:", len(pdf_files))
        print("Successful:", report["successful_documents"])
        print("Report:", report_path)


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--input",
        default="phase-1/input"
    )

    parser.add_argument(
        "--output",
        default="phase-1/reports"
    )

    args = parser.parse_args()

    engine = PDFDiagnosticEngine()

    engine.analyse_directory(
        args.input,
        args.output
    )


if __name__ == "__main__":
    main()
