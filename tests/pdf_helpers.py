from io import BytesIO

from pypdf import PdfWriter


def _valid_pdf_bytes() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.write(output)
    return output.getvalue()


VALID_PDF_BYTES = _valid_pdf_bytes()
