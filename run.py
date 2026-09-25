"""Start the personal local app (Python 3.11+)."""
import os
from pathlib import Path
import sys
import threading
import webbrowser
import urllib.request
import uvicorn
import deployment

if __name__ == '__main__':
    os.chdir(Path(__file__).resolve().parent)
    deployment.validate_deployment()
    host = '0.0.0.0' if deployment.cloud_mode() else '127.0.0.1'
    port = int(os.environ.get('PORT', '8765'))
    url = deployment.public_origin() or f'http://127.0.0.1:{port}'
    if '--open' in sys.argv and not deployment.cloud_mode():
        try:
            with urllib.request.urlopen(url+'/api/config',timeout=1) as response:
                if response.status == 200:
                    webbrowser.open(url)
                    sys.exit(0)
        except OSError:
            pass
        timer = threading.Timer(1.2, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    print(f'\nTranscribe: {url}\n')
    uvicorn.run('app:app', host=host, port=port, access_log=False, timeout_graceful_shutdown=1900)
