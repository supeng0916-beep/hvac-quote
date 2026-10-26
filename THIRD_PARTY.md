# Third-party components

HVAC Quote relies on these separately distributed projects. Their original licenses and notices continue to apply; this file does not relicense them.

| Component | Role |
| --- | --- |
| Python | Runtime |
| FastAPI, Starlette, Uvicorn, Pydantic, python-multipart | HTTP application and request handling |
| ezdxf | DXF reading and synthetic drawing generation |
| openpyxl | Excel import and output |
| ReportLab, Pillow | PDF and image handling |
| LibreOffice | Spreadsheet recalculation |
| GNU LibreDWG | Optional DWG conversion executable; built separately in Docker |
| Noto CJK / WenQuanYi fonts | Chinese rendering in the container |
| pytest, HTTPX, PyMuPDF | Development tests and output verification |

Python dependency versions are recorded in `uv.lock`; runtime installation versions are exported to `requirements.lock.txt`. Package distributions include their license metadata. System tools and fonts are installed by the Dockerfile from their upstream archives or distribution packages rather than bundled in this repository.

`src/hvac_cad` is this project's internal CAD utility package, built around ezdxf. No customer drawing samples or proprietary CAD SDK binaries are distributed here.
