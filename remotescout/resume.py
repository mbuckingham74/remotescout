from pypdf import PdfReader


def extract_resume_text(path):
    reader = PdfReader(path)
    pages = [page.extract_text() for page in reader.pages]
    return "\n".join(text for text in pages if text)
