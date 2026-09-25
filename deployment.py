"""Hosting settings. Remote deployments require a private access password."""
import base64
import binascii
import os
import secrets
from urllib.parse import urlsplit


def cloud_mode():
    return os.environ.get('APP_MODE') == 'cloud'


def public_origin():
    return os.environ.get('PUBLIC_ORIGIN','').rstrip('/') or ('https://' + os.environ['RAILWAY_PUBLIC_DOMAIN'] if os.environ.get('RAILWAY_PUBLIC_DOMAIN') else '')


def allowed_hosts():
    hosts=['localhost','127.0.0.1','[::1]','healthcheck.railway.app']
    origin=public_origin()
    if origin and urlsplit(origin).hostname: hosts.append(urlsplit(origin).hostname)
    return hosts


def validate_deployment():
    if not cloud_mode(): return
    if len(os.environ.get('APP_ACCESS_PASSWORD','')) < 24:
        raise RuntimeError('Cloud deployment requires APP_ACCESS_PASSWORD with at least 24 characters.')
    if not os.environ.get('OPENAI_API_KEY'):
        raise RuntimeError('Cloud deployment requires the OPENAI_API_KEY secret.')
    if not os.environ.get('DATA_DIR'):
        raise RuntimeError('Cloud deployment requires DATA_DIR on a persistent volume.')


def authorized(header):
    if not cloud_mode(): return True
    expected=os.environ.get('APP_ACCESS_PASSWORD','')
    if len(expected)<24: return False
    try:
        scheme,encoded=(header or '').split(' ',1)
        if scheme.lower()!='basic': return False
        username,password=base64.b64decode(encoded,validate=True).decode().split(':',1)
        return secrets.compare_digest(username.encode(),b'transcribe') and secrets.compare_digest(password.encode(),expected.encode())
    except (ValueError,UnicodeError,binascii.Error): return False
