from urllib.parse import parse_qsl, unquote, urlparse, urlunparse

IDRIVE_HOME_PATH = "/idrive/home"
IDRIVE_HOST_NAMES = {"idrive.com", "www.idrive.com"}


def is_idrive_url(url: str) -> bool:
    parsed_url = urlparse(url)
    host_name = parsed_url.hostname
    return host_name is not None and host_name.lower() in IDRIVE_HOST_NAMES


def normalized_folder_url(url: str) -> str:
    parsed_url = urlparse(url)
    path = unquote(parsed_url.path).rstrip("/")
    query = ""
    if not (is_idrive_url(url) and path.startswith(IDRIVE_HOME_PATH)):
        query_pairs = sorted(parse_qsl(parsed_url.query, keep_blank_values=True))
        query = "&".join(f"{key}={value}" for key, value in query_pairs)
    return urlunparse(
        (
            parsed_url.scheme.lower(),
            parsed_url.netloc.lower(),
            path,
            "",
            query,
            "",
        )
    )
