'''从nhentai.net下载本子并生成pdf'''

from __future__ import annotations

import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen


BASE_URL = "https://nhentai.net"
IMAGE_CDNS = (
    "https://i1.nhentai.net",
    "https://i2.nhentai.net",
    "https://i3.nhentai.net",
    "https://i4.nhentai.net",
)
#这里保存了4个url，都是图片CDNurl，但我不确定实际请求时轮询这4个是否有兜底作用，程序中如果i1能用会默认用i1

USER_AGENT = "nhentai-pdf-downloader/1.0 (AstrBot plugin)"
GALLERY_URL_PATTERN = re.compile(r"^/g/(\d+)/?$")
SAFE_FILENAME_PATTERN = re.compile(r"[^A-Za-z0-9._ -]+")
DEFAULT_PDF_PASSWORD = "114514"


class DownloadError(RuntimeError):
    '''下载错误'''


class AuthRequiredError(DownloadError):
    '''认证错误，不需要登录的话应该基本用不上'''


def gallery_id_from_url(value: str) -> str:
    '''从漫画主页 URL 读取 gallery_id'''
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"nhentai.net", "www.nhentai.net"}:
        raise DownloadError("URL 必须是 https://nhentai.net/g/漫画编号")
    match = GALLERY_URL_PATTERN.fullmatch(parsed.path)
    if not match or parsed.query or parsed.fragment:
        raise DownloadError("请输入漫画主页 URL，例如 https://nhentai.net/g/123456")
    return match.group(1)


def _read_error_body(exc: HTTPError) -> str:
    try:
        return exc.read(1024).decode("utf-8", errors="replace").strip()
    except OSError:
        return ""


def _auth_hint() -> str:
    '''不确定n站是否存在必须登录才能下载的漫画，这个函数用于兜底'''
    return (
        "服务器要求登录；请设置 NHENTAI_COOKIE 环境变量，"
        "或向 download_pdf 传入 cookie 参数。"
    )


def _request(
    url: str,
    headers: dict[str, str],
    timeout: float,
    *,
    method: str = "GET",
    attempts: int = 3,
) -> bytes:  
    '''构造请求对象以及处理连接类错误'''
    request = Request(url, method=method, headers=headers)
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read()
        except HTTPError as exc:
            detail = _read_error_body(exc)
            if exc.code in {401, 403}:
                raise AuthRequiredError(
                    f"nhentai 返回 HTTP {exc.code}。{_auth_hint()}"
                    + (f" 服务器信息: {detail[:200]}" if detail else "")
                ) from exc
            if exc.code == 404:
                raise DownloadError(f"nhentai 返回 HTTP 404，漫画可能不存在: {url}") from exc
            last_error = DownloadError(
                f"请求失败，HTTP {exc.code}: {detail[:200] or exc.reason}"
            )
        except (URLError, TimeoutError, OSError) as exc:
            last_error = exc
        if attempt < attempts:
            time.sleep(min(2.0 * attempt, 5.0))
    if isinstance(last_error, DownloadError):
        raise last_error
    raise DownloadError(f"无法连接 {url}: {last_error}") from last_error


def _json_request(url: str, headers: dict[str, str], timeout: float) -> Any:
    response_headers = {**headers, "Accept": "application/json"}
    payload = _request(url, response_headers, timeout)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DownloadError("nhentai 返回的 API 内容不是有效 JSON，接口可能已变更") from exc


def _image_url_candidates(page_url: str) -> list[str]: 
    '''这个函数用于替换图片cdn'''
    parsed = urlparse(page_url)
    candidates: list[str] = []
    for cdn in IMAGE_CDNS:
        cdn_parsed = urlparse(cdn)
        candidate_url = parsed._replace(
            scheme=cdn_parsed.scheme,
            netloc=cdn_parsed.netloc,
        ).geturl()
        candidates.append(candidate_url)
    return candidates


def _gallery_page_urls(gallery: dict[str, Any],image_cdn: str,) -> list[str]:  
    '''拼装图片url'''
    pages = gallery["pages"]
    if not pages:
        raise DownloadError("漫画没有页面")
    urls: list[str] = []
    for page in pages:
        path = page["path"]
        urls.append(urljoin(image_cdn.rstrip("/") + "/","/" + path.lstrip("/"),))
    return urls


def _safe_filename(title: str, gallery_id: str) -> str:
    title = SAFE_FILENAME_PATTERN.sub("_", title).strip(" ._")
    title = re.sub(r"\s+", " ", title)
    if not title:
        title = f"nhentai_{gallery_id}"
    return f"{title}.pdf"


def _new_work_dir(root: Path, prefix: str) -> Path:
    '''创建临时工作目录，下面通过尝试20次生成候选目录名'''
    for attempt in range(20):
        candidate = root / f".{prefix}-{os.getpid()}-{time.time_ns()}-{attempt}"
        try:
            candidate.mkdir(parents=False)
            return candidate
        except FileExistsError:
            continue
    raise OSError(f"无法在输出目录创建暂存目录: {root}")


def _gallery_title(gallery: dict[str, Any], gallery_id: str) -> str:
    '''返回的json中title是一个字典，这个函数遍历title的键来寻找漫画标题'''
    title = gallery.get("title")
    for key in ("pretty", "english", "japanese"):
        if isinstance(title.get(key), str) and title[key].strip():
            return title[key].strip()
    return f"nhentai_{gallery_id}"


def _prepare_jpeg(source: Path, destination: Path) -> tuple[int, int]:
    '''图片预处理，返回图片尺寸'''
    try:
        from PIL import Image
    except ImportError as exc:
        raise DownloadError("缺少处理图片的相关依赖 Pillow ，请先执行: python -m pip install Pillow") from exc
    try:
        with Image.open(source) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise DownloadError(f"图片尺寸无效: {source.name}")
            if image.format == "JPEG" and image.mode == "RGB":
                destination.write_bytes(source.read_bytes())
            else:
                image.convert("RGB").save(destination, "JPEG", quality=95, optimize=True)
            return width, height
    except DownloadError:
        raise
    except (OSError, ValueError) as exc:
        raise DownloadError(f"无法解析图片 {source.name}: {exc}") from exc


def _pdf_object(stream, object_number: int, body: bytes) -> int:
    '''流式写入图片或者别的'''
    offset = stream.tell()
    stream.write(f"{object_number} 0 obj\n".encode("ascii"))
    stream.write(body)
    stream.write(b"\nendobj\n")
    return offset


def write_pdf(image_paths: Iterable[Path], output_path: Path) -> None:
    '''直接写出文件，不返回数据'''
    # 按照如下树状结构写入pdf
    # 1 Catalog  -> 2 Pages
    # 2 Pages    -> [3 Page, 6 Page]
    # 3 Page     -> 4 Image, 5 Contents
    # 4 Image    -> JPEG 字节
    # 5 Contents -> 在页面上绘制 Im0
    # 6 Page     -> 7 Image, 8 Contents
    # 7 Image    -> JPEG 字节
    # 8 Contents -> 在页面上绘制 Im0
    # .......

    paths = list(image_paths)
    if not paths:
        raise DownloadError("没有可写入 PDF 的图片")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    convert_dir = _new_work_dir(output_path.parent, "nhentai-pdf")
    try:
        converted: list[tuple[Path, int, int]] = []
        for index, source in enumerate(paths, start=1):
            target = Path(convert_dir) / f"{index:05d}.jpg"
            width, height = _prepare_jpeg(source, target)
            converted.append((target, width, height))

        partial_path = output_path.with_name(output_path.name + ".part")
        offsets: list[int] = [0]
        page_count = len(converted)
        page_object_numbers = [3 + index * 3 for index in range(page_count)]
        try:
            with partial_path.open("wb") as pdf:
                pdf.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
                offsets.append(_pdf_object(pdf, 1, b"<< /Type /Catalog /Pages 2 0 R >>"))
                kids = " ".join(f"{number} 0 R" for number in page_object_numbers).encode("ascii")
                offsets.append(
                    _pdf_object(
                        pdf,
                        2,
                        b"<< /Type /Pages /Kids [" + kids + f"] /Count {page_count} >>".encode("ascii"),
                    )
                )
                for index, (jpeg_path, width, height) in enumerate(converted):
                    page_number = 3 + index * 3
                    image_number = page_number + 1
                    content_number = page_number + 2
                    content = f"q\n{width} 0 0 {height} 0 0 cm\n/Im0 Do\nQ\n".encode("ascii")
                    offsets.append(
                        _pdf_object(
                            pdf,
                            page_number,
                            (
                                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] "
                                f"/Resources << /XObject << /Im0 {image_number} 0 R >> >> "
                                f"/Contents {content_number} 0 R >>"
                            ).encode("ascii"),
                        )
                    )
                    jpeg_data = jpeg_path.read_bytes()#图片对象
                    image_header = (
                        f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                        f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode "
                        f"/Length {len(jpeg_data)} >>\nstream\n"
                    ).encode("ascii")
                    offsets.append(_pdf_object(pdf, image_number, image_header + jpeg_data + b"\nendstream"))
                    offsets.append(                  #内容流对象
                        _pdf_object(
                            pdf,
                            content_number,
                            f"<< /Length {len(content)} >>\nstream\n".encode("ascii")
                            + content
                            + b"endstream",
                        )
                    )
                xref_offset = pdf.tell()
                object_count = 3 + page_count * 3
                pdf.write(f"xref\n0 {object_count}\n".encode("ascii"))
                pdf.write(b"0000000000 65535 f \n")
                for offset in offsets[1:]:
                    pdf.write(f"{offset:010d} 00000 n \n".encode("ascii"))
                pdf.write(
                    f"trailer\n<< /Size {object_count} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
                )
            partial_path.replace(output_path)
        except OSError:
            try:
                partial_path.unlink()
            except OSError:
                pass
            raise
    finally:
        shutil.rmtree(convert_dir, ignore_errors=True)


def encrypt_pdf(pdf_path: Path, password: str = DEFAULT_PDF_PASSWORD) -> None:
    '''使用 AES-256 对现有 PDF 进行加密。'''
    password = str(password or DEFAULT_PDF_PASSWORD)
    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as exc:
        raise DownloadError(
            "缺少 PDF 加密依赖 pypdf，请先执行: python -m pip install pypdf"
        ) from exc
    encrypted_path = pdf_path.with_name(pdf_path.name + ".encrypted.part")
    try:
        with pdf_path.open("rb") as source:
            reader = PdfReader(source)
            writer = PdfWriter()
            writer.append_pages_from_reader(reader)
            if reader.metadata:
                writer.add_metadata(
                    {
                        str(key): str(value)
                        for key, value in reader.metadata.items()
                        if value is not None
                    }
                )
            writer.encrypt(
                user_password=password,
                owner_password=password,
                algorithm="AES-256",
            )
            with encrypted_path.open("wb") as destination:
                writer.write(destination)
        encrypted_path.replace(pdf_path)
    except Exception as exc:
        try:
            encrypted_path.unlink()
        except OSError:
            pass
        raise DownloadError(f"无法加密 PDF: {exc}") from exc


def _build_headers(cookie: str | None, referer: str) -> dict[str, str]:
    '''nh的请求头只可能有cookie这一种认证参数，一般来说cookie也不需要'''
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": referer,
    }
    if cookie:
        headers["Cookie"] = cookie  #请求头的认证参数（大概率不需要）
    return headers


def download_gallery(
    gallery_id: str,
    image_dir: Path,
    timeout: float,
    cookie: str | None,
) -> tuple[list[Path], dict[str, Any]]:
    gallery_url = f"{BASE_URL}/g/{gallery_id}/"
    headers = _build_headers(cookie, gallery_url)
    gallery = _json_request(f"{BASE_URL}/api/v2/galleries/{gallery_id}",headers,timeout,)
    if not isinstance(gallery, dict):
        raise DownloadError("漫画 API 返回的内容不是字典")
    page_urls = _gallery_page_urls(gallery,IMAGE_CDNS[0]) #默认先从i1开始下载，这里就是先取成i1，如果i1下载不了，后面的_image_url_candidates会替换
    image_paths: list[Path] = []
    for index, page_url in enumerate(page_urls, start=1):
        target = image_dir / f"{index:05d}.img"
        errors: list[str] = []  #接下来从i1到i4查找（一般i1可以用就直接用了）
        for candidate_url in _image_url_candidates(page_url):
            try:
                candidate_data = _request(candidate_url, {**headers, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}, timeout)
            except AuthRequiredError:
                # 如果登录失败，换成i2、i3也没用，所以直接抛错
                raise
            except DownloadError as exc:
                errors.append(f"{candidate_url}: {exc}")
                continue
            if not candidate_data.startswith((b"\xff\xd8", b"\x89PNG", b"GIF8", b"RIFF")):
                errors.append(f"{candidate_url}: 返回内容不是有效图片")
                continue
            data = candidate_data
            break
        else:
            details = "\n".join(errors)
            raise DownloadError(f"第 {index} 页在所有图片 CDN 上均下载失败：\n"f"{details}")
        target.write_bytes(data)
        image_paths.append(target)
        time.sleep(0.2)
    return image_paths, gallery


def _output_path(path_arg: Path, gallery_id: str, gallery: dict[str, Any]) -> Path:
    expanded = path_arg.expanduser()
    if expanded.suffix.lower() == ".pdf":
        return expanded.resolve()
    return (expanded / _safe_filename(_gallery_title(gallery, gallery_id), gallery_id)).resolve()


def gallery_id_from_value(value: str) -> str:
    """将纯数字漫画编号或 nhentai 漫画主页 URL 规范化为漫画编号"""
    text = str(value).strip()
    if re.fullmatch(r"\d+", text):
        return text
    return gallery_id_from_url(text)


def download_pdf(
    gallery: str,
    output_dir: Path,
    timeout: float = 60,
    cookie: str | None = None,
    pdf_password: str | None = None,
) -> Path:
    """下载一部漫画并返回加密 PDF 路径，供 AstrBot 插件导入调用。"""
    gallery_id = gallery_id_from_value(gallery)
    if timeout <= 0:
        raise DownloadError("timeout must be greater than zero")

    cookie = cookie or os.environ.get("NHENTAI_COOKIE")

    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    staging_dir = _new_work_dir(output_root, f"nhentai-{gallery_id}")
    try:
        images, gallery_payload = download_gallery(
            gallery_id,
            staging_dir,
            timeout,
            cookie,
        )
        output_path = _output_path(output_root, gallery_id, gallery_payload)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        write_pdf(images, output_path)
        encrypt_pdf(
            output_path,
            pdf_password
            or os.environ.get("NHENTAI_PDF_PASSWORD")
            or DEFAULT_PDF_PASSWORD,
        )
        return output_path
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
