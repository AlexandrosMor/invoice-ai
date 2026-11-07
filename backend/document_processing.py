"""PDF/image preprocessing. Text is useful context, never treated as a table parser."""
from dataclasses import dataclass
from pathlib import Path
import io
import fitz
from PIL import Image, ImageOps

@dataclass
class ProcessedPage:
    number: int
    image: bytes
    text: str
    digital: bool
    spans: list

@dataclass
class ProcessedDocument:
    pages: list[ProcessedPage]
    document_kind: str
    source_mime: str

    @property
    def all_digital(self) -> bool:
        return bool(self.pages) and all(p.digital for p in self.pages)

class DocumentProcessor:
    MAX_PAGES = 30
    @classmethod
    def process(cls, path: Path, mime: str) -> ProcessedDocument:
        if mime == "application/pdf":
            try:
                with fitz.open(path) as doc:
                    if doc.is_encrypted and not doc.authenticate(""):
                        raise ValueError("Password-protected PDFs are not supported")
                    if not doc.page_count: raise ValueError("PDF has no pages")
                    if doc.page_count > cls.MAX_PAGES: raise ValueError(f"PDF exceeds {cls.MAX_PAGES} pages")
                    pages=[]
                    for index, page in enumerate(doc):
                        text=page.get_text("text", sort=True).strip()
                        # get_pixmap respects page rotation; raster remains authoritative for layout.
                        scale=min(1.7,2500/max(float(page.rect.width),float(page.rect.height)))
                        image=page.get_pixmap(matrix=fitz.Matrix(scale,scale), alpha=False).tobytes("png")
                        spans=[]
                        for block in page.get_text("dict",sort=True).get("blocks",[]):
                            for line in block.get("lines",[]):
                                for span in line.get("spans",[]):
                                    if span.get("text","").strip(): spans.append({"text":span["text"],"bbox":[round(float(n),2) for n in span["bbox"]]})
                        pages.append(ProcessedPage(index+1,image,text,len(text)>=35,spans))
            except (fitz.FileDataError, fitz.PDFException) as e:
                raise ValueError(f"Unreadable or malformed PDF: {e}") from e
            if not any(p.digital or p.image for p in pages): raise ValueError("PDF contains no readable pages")
            digital_count=sum(p.digital for p in pages)
            kind="digital_pdf" if digital_count==len(pages) else "scanned_pdf" if digital_count==0 else "mixed_pdf"
            return ProcessedDocument(pages,kind,mime)
        try:
            with Image.open(path) as source:
                source.verify()
            with Image.open(path) as source:
                image=ImageOps.exif_transpose(source).convert("RGB")
                output=io.BytesIO(); image.save(output,format="PNG")
            return ProcessedDocument([ProcessedPage(1,output.getvalue(),"",False,[])],"image",mime)
        except Exception as e:
            raise ValueError(f"Unsupported or unreadable image: {e}") from e

    # Backwards-compatible convenience used by existing callers.
    @classmethod
    def images(cls, path: Path, mime: str) -> list[bytes]:
        return [p.image for p in cls.process(path,mime).pages]
