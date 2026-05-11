"""Cached package index server."""

import os
import gzip
import zlib
import typing as t
import logging
import urllib.parse
import concurrent.futures

import flask
import jinja2
import werkzeug.exceptions

from . import _cache

try:
    import colored_traceback
except ImportError:  # pragma: no cover
    pass
else:  # pragma: no cover
    colored_traceback.add_hook()

logging_level = os.environ.get("PROXPI_LOGGING_LEVEL", "INFO")
fmt = "%(asctime)s [%(levelname)8s] %(name)s: %(message)s"
try:
    import coloredlogs
except ImportError:  # pragma: no cover
    logging.basicConfig(level=logging_level, format=fmt)
else:  # pragma: no cover
    coloredlogs.install(
        level=logging_level,
        fmt=fmt,
        field_styles={
            "asctime": {"faint": True, "color": "white"},
            "levelname": {"bold": True, "color": "blue"},
            "name": {"bold": True, "color": "yellow"},
        },
    )
logger = logging.getLogger(__name__)

_proxpi_version = _cache.get_proxpi_version()
logger.info(f"proxpi version: {_proxpi_version or '<unknown>'}")

try:
    import gunicorn.glogging
except ImportError:
    gunicorn = None
else:

    class _GunicornLogger(gunicorn.glogging.Logger):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.error_log.propagate = True
            self.access_log.propagate = True

        def _set_handler(self, *_, **__):
            pass


app = flask.Flask("proxpi")
app.jinja_loader = jinja2.PackageLoader("proxpi")
cache = _cache.Cache.from_config()
if app.debug or app.testing:
    logging.root.setLevel(logging.DEBUG)
    for handler in logging.root.handlers:
        if handler.level > logging.DEBUG:
            handler.level = logging.DEBUG
logger.info("Cache: %r", cache)
KNOWN_LATEST_JSON_VERSION = "v1"
KNOWN_DATASET_KEYS = ["requires-python", "dist-info-metadata", "gpg-sig", "yanked"]


def _wants_json(version: str = "v1") -> bool:
    """Determine if client wants a JSON response.

    First checks `format` request query paramater, and if its value is a
    known content-type, decides if client wants JSON. Then falls back to
    HTTP content-negotiation, where the decision is based on the quality
    of the JSON content-type (JSON must be equally or more preferred to
    HTML, but strictly more preferred to 'text/html').

    Args:
        version: PyPI JSON response content-type version
    """

    if version == KNOWN_LATEST_JSON_VERSION:
        try:
            wants_json = _wants_json("latest")
        except werkzeug.exceptions.NotAcceptable:
            pass
        else:
            if wants_json:
                return True

    json_key = f"application/vnd.pypi.simple.{version}+json"
    html_keys = {
        "text/html",
        "application/vnd.pypi.simple.v1+html",
        "application/vnd.pypi.simple.latest+html",
    }

    if flask.request.args.get("format"):
        if flask.request.args["format"] == json_key:
            return True
        elif flask.request.args["format"] in html_keys:
            return False

    json_quality = flask.request.accept_mimetypes.quality(json_key)
    html_quality = max(flask.request.accept_mimetypes.quality(k) for k in html_keys)
    iana_html_quality = flask.request.accept_mimetypes.quality("text/html")

    if not json_quality and not html_quality:
        flask.abort(406)
    return (
        json_quality
        and json_quality >= html_quality
        and json_quality > iana_html_quality
    )


def _build_json_response(data: dict, version: str = "v1") -> flask.Response:
    response = flask.jsonify(data)
    response.mimetype = f"application/vnd.pypi.simple.{version}+json"
    return response


BINARY_FILE_MIME_TYPE = (
    os.environ.get("PROXPI_BINARY_FILE_MIME_TYPE", "")
).lower() not in ("", "0", "no", "off", "false")
_file_mime_type = "application/octet-stream" if BINARY_FILE_MIME_TYPE else None

# Parallel range-request download settings (helps when the upstream path has
# per-flow throttling and a single TCP connection can't fill the pipe).
PARALLEL_CONNECTIONS = int(os.environ.get("PROXPI_PARALLEL_CONNECTIONS", "1"))
# Minimum file size (bytes) before we bother with parallel ranges
PARALLEL_MIN_SIZE = int(os.environ.get("PROXPI_PARALLEL_MIN_SIZE", str(2 * 1024 * 1024)))


def _parallel_download_bytes(url: str, num_connections: int) -> bytes:
    """Fetch ``url`` using ``num_connections`` parallel HTTP range requests.

    Falls back to a single GET if the server doesn't advertise byte ranges,
    if the file is small, or if the HEAD probe fails.
    """

    session = cache.file_cache.session
    head = session.head(url, allow_redirects=True, timeout=30)
    head.raise_for_status()

    accept_ranges = head.headers.get("Accept-Ranges", "").lower()
    content_length = head.headers.get("Content-Length")

    if accept_ranges != "bytes" or not content_length:
        logger.debug("Range requests unsupported, single GET for %s", _cache._mask_password(url))
        r = session.get(url, timeout=300)
        r.raise_for_status()
        return r.content

    size = int(content_length)
    if size < PARALLEL_MIN_SIZE or num_connections <= 1:
        r = session.get(url, timeout=300)
        r.raise_for_status()
        return r.content

    n = num_connections
    chunk_size = size // n
    chunks: t.List[t.Union[bytes, None]] = [None] * n

    def fetch(idx: int, start: int, end: int) -> None:
        resp = session.get(
            url, headers={"Range": f"bytes={start}-{end}"}, timeout=300
        )
        resp.raise_for_status()
        chunks[idx] = resp.content

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        futures = []
        for i in range(n):
            start = i * chunk_size
            end = (i + 1) * chunk_size - 1 if i < n - 1 else size - 1
            futures.append(ex.submit(fetch, i, start, end))
        for f in futures:
            f.result()

    return b"".join(chunks)  # type: ignore[arg-type]


def _parallel_proxy(url: str) -> flask.Response:
    """Serve ``url`` to the client after fetching with parallel range requests."""
    try:
        data = _parallel_download_bytes(url, PARALLEL_CONNECTIONS)
    except Exception as e:
        logger.warning(
            "Parallel download failed for %s (%s); redirecting",
            _cache._mask_password(url),
            e,
        )
        return flask.redirect(url)
    return flask.Response(data, mimetype=_file_mime_type or "application/octet-stream")


def _compress(response: t.Union[str, flask.Response]) -> flask.Response:
    response = flask.make_response(response)
    gzip_quality = flask.request.accept_encodings.quality("gzip")
    zlib_quality = flask.request.accept_encodings.quality("deflate")
    identity_quality = flask.request.accept_encodings.quality("identity")
    if gzip_quality and gzip_quality >= max(identity_quality, zlib_quality):
        response.data = gzip.compress(response.data)
        response.content_encoding = "gzip"
    elif zlib_quality and zlib_quality >= identity_quality:
        response.data = zlib.compress(response.data)
        response.content_encoding = "deflate"
    elif "identity" in flask.request.accept_encodings and not identity_quality:
        flask.abort(406)
    response.vary.add("Accept-Encoding")
    return response


@app.route("/")
def index():
    """Home page."""
    max_age = app.get_send_file_max_age("index.html")
    return flask.send_from_directory(
        os.path.join(app.root_path, app.template_folder), "index.html", max_age=max_age
    )


@app.route("/index/")
def list_packages():
    """List all projects in index(es)."""
    package_names = cache.list_projects()
    if _wants_json():
        response = _build_json_response(data={
            "meta": {"api-version": "1.0"},
            "projects": [{"name": n} for n in package_names],
        })  # fmt: skip
    else:
        response = flask.make_response(
            flask.render_template("packages.html", package_names=package_names),
        )
    response.vary.add("Accept")
    return _compress(response)


@app.route("/index/<package_name>/")
def list_files(package_name: str):
    """List all files for a project."""
    try:
        files, versions = cache.list_files(package_name)
    except _cache.NotFound:
        flask.abort(404)
        raise

    if _wants_json():
        files_data = []
        for file in files:
            file_data = file.to_json_response()
            file_data["url"] = file.name
            files_data.append(file_data)

        response_data = {
            "meta": {"api-version": "1.0"},
            "name": package_name,
            "files": files_data,
        }

        if versions is not None:
            response_data["versions"] = versions
            if all(f.size or f.size == 0 for f in files):
                response_data["meta"]["api-version"] = "1.1"
            # No need to remove versions, size and upload-time for API < v1.1

        response = _build_json_response(data=response_data)

    else:
        response = flask.make_response(
            flask.render_template("files.html", package_name=package_name, files=files),
        )

    response.vary.add("Accept")
    return _compress(response)


@app.route("/index/<package_name>/<file_name>")
def get_file(package_name: str, file_name: str):
    """Download a file."""
    try:
        path = cache.get_file(package_name, file_name)
    except _cache.NotFound:
        flask.abort(404)
        raise
    scheme = urllib.parse.urlparse(path).scheme
    if scheme and scheme != "file":
        if PARALLEL_CONNECTIONS > 1:
            return _parallel_proxy(path)
        return flask.redirect(path)
    response = flask.send_file(path, mimetype=_file_mime_type)
    if (
        response.content_encoding == "gzip"
        and response.content_type == "application/x-tar"
    ):
        del response.content_encoding
        response.content_type = "application/x-tar+gzip"
    return response


@app.route("/cache/list", methods=["DELETE"])
def invalidate_list():
    """Invalidate project list cache."""
    cache.invalidate_list()
    return {"status": "success", "data": None}


@app.route("/cache/<package_name>", methods=["DELETE"])
def invalidate_package(package_name):
    """Invalidate project file list cache."""
    cache.invalidate_project(package_name)
    return {"status": "success", "data": None}


@app.route("/health")
def health():
    return {"status": "success", "data": None}
