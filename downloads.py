"""Public platform downloads; no account credentials or browser cookies."""
import asyncio
from pathlib import Path
import re
import secrets
import shutil
import sys
import tempfile
import time
from urllib.parse import urlsplit, parse_qs
import imageio_ffmpeg
from core import UserError, MAX_BYTES, public_addresses, validate_url

records = {}


def platform_url(raw):
    url = validate_url(raw)
    u = urlsplit(url)
    host = u.hostname
    if host in ('youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'):
        if u.path == '/watch' and re.fullmatch(r'[\w-]{11}',parse_qs(u.query).get('v',[''])[0]): return url
        if re.fullmatch(r'/(shorts|embed)/[\w-]{11}/?',u.path): return url
    if host == 'youtu.be' and re.fullmatch(r'/[\w-]{11}/?',u.path): return url
    if host in ('instagram.com','www.instagram.com','m.instagram.com') and re.fullmatch(r'/(reel|reels|p|tv)/[\w-]+/?',u.path): return url
    if host in ('tiktok.com','www.tiktok.com') and re.fullmatch(r'/@[^/]+/video/\d+/?',u.path): return url
    if host in ('vm.tiktok.com','vt.tiktok.com','www.tiktok.com') and re.fullmatch(r'/(?:t/)?[A-Za-z0-9]+/?',u.path): return url
    raise UserError('Cole o link de um vídeo do Instagram, TikTok ou YouTube. Perfis e playlists não são aceitos.', 'invalid_url')


def cleanup(all_records=False):
    for key, record in list(records.items()):
        if all_records or record['status'] in ('done','error') and time.monotonic()-record['updated']>3600:
            shutil.rmtree(record['folder'],ignore_errors=True)
            records.pop(key,None)


def reserve(raw):
    url=platform_url(raw)
    cleanup()
    if any(r['status']=='working' for r in records.values()):
        raise UserError('Já existe um download em andamento. Aguarde a conclusão.', 'busy',409)
    if len(records)>=5:
        key=min(records,key=lambda k:records[k]['updated'])
        shutil.rmtree(records.pop(key)['folder'],ignore_errors=True)
    key=secrets.token_urlsafe(24)
    records[key]={'status':'working','detail':'Acessando o vídeo…','updated':time.monotonic(), 'folder':Path(tempfile.mkdtemp(prefix='transcribe-download-'))}
    return key,url


def command(url,folder):
    args=[sys.executable,'-m','yt_dlp','--ignore-config','--no-cache-dir','--no-plugin-dirs',
          '--no-playlist','--playlist-end','1','--socket-timeout','20','--retries','1','--fragment-retries','1',
          '--max-filesize',str(MAX_BYTES),'--match-filters','!is_live & duration <=? 1800',
          '--use-extractors','youtube,instagram,tiktok,tiktokvm',
          '--ffmpeg-location',imageio_ffmpeg.get_ffmpeg_exe(),
          '-f','bv*+ba/b', '--merge-output-format','mp4',
          '--newline','--progress','--no-warnings','--no-write-info-json',
          '-o',str(folder/'video.%(ext)s')]
    node=shutil.which('node')
    bundled=Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node'
    if not node and bundled.is_file(): node=str(bundled)
    if node: args += ['--js-runtimes','node:'+node]
    return args+['--',url]


def error_message(output):
    text=output.lower()
    if any(x in text for x in ('max-filesize','larger than max','filesize is larger')): return 'Este vídeo ultrapassa 300 MB. Tente uma versão menor.'
    if any(x in text for x in ('does not pass filter','duration','live event')): return 'Escolha um vídeo gravado de até 30 minutos. Transmissões ao vivo não são aceitas.'
    return 'A plataforma não liberou o download deste vídeo. Ele pode estar privado, indisponível ou exigir acesso pelo aplicativo original. Tente outro link ou envie o arquivo que você já tem.'


async def compatible_mp4(source, destination):
    """Normalize actual codecs, not only the extension, for QuickTime/mobile."""
    process = await asyncio.create_subprocess_exec(
        imageio_ffmpeg.get_ffmpeg_exe(), '-nostdin', '-hide_banner', '-loglevel', 'error',
        '-y', '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?',
        '-c:v', 'libx264', '-preset', 'fast', '-crf', '21', '-pix_fmt', 'yuv420p',
        '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2', '-threads', '2',
        '-c:a', 'aac', '-b:a', '160k', '-movflags', '+faststart',
        '-fs', str(MAX_BYTES + 1), str(destination),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        async with asyncio.timeout(1200):
            _, error = await process.communicate()
        if process.returncode or not destination.exists() or not destination.stat().st_size:
            raise UserError('Não foi possível preparar um MP4 compatível. Tente outro vídeo.', 'conversion')
        if destination.stat().st_size > MAX_BYTES:
            raise UserError('O vídeo convertido ultrapassa 300 MB. Escolha um vídeo menor.', 'too_large')
        return destination
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def run(key,url):
    record=records[key]; folder=record['folder']; process=None; output=''
    try:
        await asyncio.to_thread(public_addresses,urlsplit(url).hostname)
        process=await asyncio.create_subprocess_exec(*command(url,folder),stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.STDOUT,limit=1024*1024)
        async with asyncio.timeout(420):
            async def watch_size():
                while process.returncode is None:
                    if sum(p.stat().st_size for p in folder.iterdir() if p.is_file())>MAX_BYTES*2:
                        process.kill()
                        raise UserError('O download ultrapassou o tamanho permitido de 300 MB.', 'too_large')
                    await asyncio.sleep(.5)
            async def read_output():
                nonlocal output
                while line:=await process.stdout.readline():
                    text=line.decode(errors='replace');output=(output+text)[-12000:]
                    if '[download]' in text: record['detail']='Baixando vídeo…'
                    if '[Merger]' in text: record['detail']='Preparando arquivo…'
                await process.wait()
            watch=asyncio.create_task(watch_size())
            try:
                reader=asyncio.create_task(read_output())
                done,_=await asyncio.wait([reader,watch],return_when=asyncio.FIRST_COMPLETED)
                for task in done: task.result()
                await reader
            finally:
                watch.cancel()
                if not reader.done():reader.cancel()
                await asyncio.gather(watch,reader,return_exceptions=True)
        files=[p for p in folder.iterdir() if p.suffix.lower() in ('.mp4','.webm','.mkv','.mov') and p.is_file()]
        if process.returncode or len(files)!=1 or not files[0].stat().st_size:
            raise UserError(error_message(output),'unavailable')
        if files[0].stat().st_size>MAX_BYTES: raise UserError('Este vídeo ultrapassa 300 MB. Tente outro vídeo.', 'too_large')
        record['detail']='Convertendo para MP4 compatível…'
        ready = await compatible_mp4(files[0], folder/'compatible.mp4')
        record.update(status='done',detail='MP4 pronto para salvar.',file=ready,size=ready.stat().st_size)
    except asyncio.CancelledError:
        record.update(status='error',detail='Download interrompido. Tente novamente.')
        raise
    except TimeoutError:
        record.update(status='error',detail='O download demorou demais. Tente novamente com um vídeo menor.')
    except UserError as exc: record.update(status='error',detail=exc.message)
    except Exception: record.update(status='error',detail='Não foi possível baixar o vídeo. Tente novamente.')
    finally:
        if process and process.returncode is None:
            process.kill();await process.wait()
        record['updated']=time.monotonic()
        if record['status']!='done':shutil.rmtree(folder,ignore_errors=True)
