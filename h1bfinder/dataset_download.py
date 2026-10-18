"""Bounded downloads from explicitly trusted USCIS hosts only."""
import ipaddress
import socket
from urllib.parse import urlsplit, unquote
import requests

TRUSTED_HOSTS = {'www.uscis.gov', 'uscis.gov'}
MAX_BYTES = 25 * 1024 * 1024


def validate_url(url, *, dataset=True):
    if not isinstance(url, str) or not url or any(ord(c) < 33 or ord(c) == 127 for c in url) or '\\' in url:
        raise ValueError('Use a direct HTTPS download link from www.uscis.gov.')
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == 'https' and parsed.hostname in TRUSTED_HOSTS
                 and parsed.port in (None, 443) and not parsed.username and not parsed.password
                 and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Only HTTPS links on www.uscis.gov or uscis.gov are allowed.')
    if dataset:
        path = unquote(parsed.path).lower()
        if not path.startswith('/sites/default/files/') or any(p in ('.','..') for p in path.split('/')) or not path.endswith(('.csv','.xlsx','.xls','.zip')):
            raise ValueError('Use a direct USCIS CSV, XLSX, XLS or ZIP file link, or upload a downloaded file.')
    return parsed


def download(url, *, dataset=True, head=False):
    parsed = validate_url(url, dataset=dataset)
    # Fail closed for hosts resolving to loopback/private/link-local addresses.
    addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
        raise ValueError('The download host must resolve only to public addresses.')
    with requests.Session() as client:
        # Do not inherit a proxy or credentials from the server environment.
        client.trust_env = False
        with client.request('HEAD' if head else 'GET', url, headers={'User-Agent':'h1b-job-finder/1.0'},
                            timeout=(10,30), allow_redirects=False, stream=True) as response:
            if 300 <= response.status_code < 400:
                raise ValueError('Redirects are not followed. Use the final direct USCIS file link or upload the file.')
            response.raise_for_status()
            if head:
                return b''
            if int(response.headers.get('Content-Length', 0)) > MAX_BYTES:
                raise ValueError('Download exceeds the 25 MB limit.')
            data = bytearray()
            for chunk in response.iter_content(chunk_size=64*1024):
                data.extend(chunk)
                if len(data) > MAX_BYTES:
                    raise ValueError('Download exceeds the 25 MB limit.')
            return bytes(data)
