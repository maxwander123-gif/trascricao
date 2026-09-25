import asyncio
from contextlib import asynccontextmanager
import logging
from pathlib import Path
import secrets
import shutil
import tempfile
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
import httpx
import json
import history
import editorial
import downloads
import deployment

creative_busy = set()
uploads = {}

from core import (ROOT, MAX_BYTES, EXTENSIONS, UserError, settings, validate_url,
                  resolve_media, download, extract_audio, transcribe)

jobs = {}
tasks = set()
csrf = secrets.token_urlsafe(32)
log = logging.getLogger('transcribe')


def prune():
    now = time.monotonic()
    for key, upload in list(uploads.items()):
        if not upload['busy'] and now - upload['updated'] > 3600:
            shutil.rmtree(upload['folder'], ignore_errors=True)
            uploads.pop(key, None)
            jobs.pop(key, None)
    for key, value in list(jobs.items()):
        if value['status'] in ('done', 'error') and now - value['updated'] > 3600:
            jobs.pop(key, None)


@asynccontextmanager
async def lifespan(app):
    deployment.validate_deployment()
    async def cleanup():
        while True:
            await asyncio.sleep(60)
            prune()
            downloads.cleanup()
    cleaner = asyncio.create_task(cleanup())
    yield
    cleaner.cancel()
    for task in list(tasks):
        task.cancel()
    await asyncio.gather(cleaner, *tasks, return_exceptions=True)
    downloads.cleanup(all_records=True)
    for upload in uploads.values(): shutil.rmtree(upload['folder'], ignore_errors=True)
    uploads.clear()


app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=deployment.allowed_hosts())


@app.middleware('http')
async def protect(request, call_next):
    if request.url.path != '/healthz' and not deployment.authorized(request.headers.get('authorization')):
        return JSONResponse({'message':'Acesso privado.'}, status_code=401, headers={'WWW-Authenticate':'Basic realm="Transcribe", charset="UTF-8"', 'Cache-Control':'no-store'})
    if request.method == 'POST':
        if request.headers.get('x-transcribe-token') != csrf:
            return JSONResponse({'message':'Atualize a página e tente novamente.'}, status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != (deployment.public_origin() or str(request.base_url).rstrip('/')):
            return JSONResponse({'message':'Abra o aplicativo neste computador.'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    return response


@app.exception_handler(UserError)
async def friendly(request, exc):
    return JSONResponse({'message':exc.message, 'code':exc.code}, status_code=exc.status)


@app.exception_handler(Exception)
async def unexpected(request, exc):
    log.error('Request failed: %s', type(exc).__name__)
    return JSONResponse({'message':'Não foi possível concluir a solicitação. Tente novamente.'}, status_code=500)


@app.get('/healthz')
async def health():
    return {'status':'ok'}


@app.get('/')
async def index():
    return FileResponse(ROOT / 'static/index.html')


@app.get('/api/config')
async def config():
    values = settings()
    return {'ready': bool(values.get('OPENAI_API_KEY')), 'instagram': bool(values.get('INSTAGRAM_ACCESS_TOKEN') and values.get('INSTAGRAM_USER_ID')), 'token':csrf, 'maxMB':MAX_BYTES // (1024 * 1024)}


async def bounded_body(request, limit, destination=None):
    size, chunks = 0, []
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise UserError('O arquivo ultrapassa o tamanho permitido. O limite é 300 MB.', 'too_large', 413)
        if destination:
            destination.write(chunk)
        else:
            chunks.append(chunk)
    if not size:
        raise UserError('Selecione um arquivo com conteúdo ou cole um link.', 'empty')
    return b''.join(chunks)


@app.post('/api/uploads')
async def begin_upload(request: Request):
    prune()
    if any(j['status'] not in ('done', 'error') for j in jobs.values()):
        raise UserError('Já existe uma transcrição em andamento.', 'busy', 409)
    try:
        data = json.loads(await bounded_body(request, 8192))
        filename, size = data['name'], data['size']
        if not isinstance(filename, str) or type(size) is not int or size < 1 or size > MAX_BYTES:
            raise ValueError()
        if Path(filename).suffix.lower() not in EXTENSIONS: raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise UserError('Envie um vídeo ou áudio válido de até 300 MB.', 'invalid_upload')
    # Recheck after reading the request, before reserving the processing slot.
    if any(j['status'] not in ('done', 'error') for j in jobs.values()):
        raise UserError('Já existe uma transcrição em andamento.', 'busy', 409)
    key = secrets.token_urlsafe(24)
    folder = Path(tempfile.mkdtemp(prefix='transcribe-'))
    source = folder / ('input' + Path(filename).suffix.lower())
    source.touch()
    uploads[key] = dict(folder=folder, source=source, size=size, received=0,
                        busy=False, updated=time.monotonic())
    jobs[key] = dict(status='receiving', detail='Recebendo arquivo…',
                     updated=time.monotonic(), title=Path(filename).name)
    return JSONResponse({'id':key}, status_code=201)


@app.post('/api/uploads/{key}/chunk')
async def upload_chunk(key: str, request: Request):
    upload = uploads.get(key)
    if upload is None: raise UserError('O envio expirou. Envie novamente.', 'not_found', 404)
    if upload['busy']: raise UserError('Aguarde o envio atual.', 'busy', 409)
    try: offset = int(request.headers.get('x-upload-offset', '-1'))
    except ValueError: offset = -1
    if offset != upload['received']:
        raise UserError('A ordem do envio não corresponde. Envie novamente.', 'offset', 409)
    upload['busy'] = True
    try:
        remaining = upload['size'] - offset
        if remaining <= 0: raise UserError('O arquivo já foi recebido.', 'complete', 409)
        chunk = await asyncio.wait_for(bounded_body(request, min(8*1024*1024, remaining)), 180)
        with upload['source'].open('ab') as file: file.write(chunk)
        upload['received'] += len(chunk)
        upload['updated'] = time.monotonic()
        return {'received':upload['received']}
    finally: upload['busy'] = False


@app.post('/api/uploads/{key}/finish')
async def finish_upload(key: str):
    upload = uploads.get(key)
    if upload is None: raise UserError('O envio expirou. Envie novamente.', 'not_found', 404)
    if upload['busy'] or upload['received'] != upload['size']:
        raise UserError('Aguarde o envio completo do arquivo.', 'incomplete', 409)
    values = settings()
    if not values.get('OPENAI_API_KEY'): raise UserError('OpenAI não configurada.', 'not_configured', 503)
    uploads.pop(key)
    jobs[key].update(status='preparing', detail='Preparando vídeo…')
    task = asyncio.create_task(process(key, None, upload['source'], upload['folder'], values))
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return JSONResponse({'id':key}, status_code=202)


@app.post('/api/uploads/{key}/cancel')
async def cancel_upload(key: str):
    upload = uploads.get(key)
    if upload and upload['busy']: raise UserError('Aguarde o envio atual.', 'busy', 409)
    if upload:
        uploads.pop(key)
        jobs.pop(key, None)
        shutil.rmtree(upload['folder'], ignore_errors=True)
    return {'ok':True}


@app.post('/api/jobs')
async def create_job(request: Request):
    prune()
    if any(j['status'] not in ('done', 'error') for j in jobs.values()):
        raise UserError('Já existe uma transcrição em andamento. Aguarde a conclusão.', 'busy', 409)
    if len(jobs) >= 40:
        oldest = min(jobs, key=lambda key: jobs[key]['updated'])
        jobs.pop(oldest)
    # Reserve before reading upload: the local app processes one file at a time.
    key = secrets.token_urlsafe(24)
    job = {'status':'receiving','detail':'Recebendo arquivo…','updated':time.monotonic()}
    jobs[key] = job
    folder = None
    try:
        url, filename = None, request.headers.get('x-file-name')
        if filename is None:
            import json
            try:
                body = json.loads(await bounded_body(request, 8192))
                if not isinstance(body, dict) or not isinstance(body.get('url'), str):
                    raise ValueError()
                url = validate_url(body['url'])
            except (ValueError, UnicodeError):
                raise UserError('Não conseguimos identificar esse link. Verifique a URL e tente novamente.', 'invalid_url')
        else:
            from urllib.parse import unquote
            filename = unquote(filename)
            if Path(filename).suffix.lower() not in EXTENSIONS:
                raise UserError('Envie MP4, MOV, M4V, WEBM, MP3, M4A ou WAV.', 'unsupported_file')
        values = settings()
        # Explain Instagram access before asking for an unrelated OpenAI key.
        if url and urlsplit(url).hostname in ('instagram.com','www.instagram.com','m.instagram.com') and not (values.get('INSTAGRAM_ACCESS_TOKEN') and values.get('INSTAGRAM_USER_ID')):
            await resolve_media(url, values)
        if not values.get('OPENAI_API_KEY'):
            raise UserError('Falta conectar a OpenAI. Salve sua chave no arquivo .env do aplicativo, no campo OPENAI_API_KEY, e tente novamente.', 'not_configured', 503)
        folder = Path(tempfile.mkdtemp(prefix='transcribe-'))
        source = folder / ('input' + (Path(filename).suffix.lower() if filename else '.media'))
        if filename is not None:
            with source.open('wb') as file:
                await asyncio.wait_for(bounded_body(request, MAX_BYTES, file), 600)
        job.update(status='preparing', detail='Preparando vídeo…', title=Path(filename).name if filename else urlsplit(url).hostname + urlsplit(url).path)
        task = asyncio.create_task(process(key, url, source, folder, values))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return JSONResponse({'id':key}, status_code=202)
    except BaseException:
        jobs.pop(key, None)
        if folder:
            shutil.rmtree(folder, ignore_errors=True)
        raise


async def process(key, url, source, folder, values):
    job = jobs[key]
    def stage(status, detail):
        job.update(status=status, detail=detail, updated=time.monotonic())
    def partial(result):
        job['result'] = result
        history.save(key, job.get('title', 'Transcrição'), result)
    try:
        async with asyncio.timeout(1800):
            if url:
                media_url = await resolve_media(url, values)
                await asyncio.to_thread(download, media_url, source)
            stage('extracting', 'Extraindo áudio…')
            audio, duration = await extract_audio(source, folder)
            result = await transcribe(audio, folder, values, stage, partial)
            result['duration'] = round(duration)
            job['result'] = result
            history.save(key, job.get('title', 'Transcrição'), result)
            stage('done', 'Tudo pronto ✓')
    except UserError as exc:
        job.update(message=exc.message, code=exc.code)
        stage('error', 'Não foi possível concluir')
    except (TimeoutError, httpx.TimeoutException):
        job.update(message='O processamento demorou mais que o esperado. Tente um vídeo menor.', code='timeout')
        stage('error', 'Tempo de espera excedido')
    except asyncio.CancelledError:
        stage('error', 'Processamento interrompido')
        raise
    except Exception as exc:
        # Do not log URLs, transcripts, provider responses or credentials.
        log.error('Processing failed: %s', type(exc).__name__)
        job.update(message='Não foi possível concluir a transcrição. Tente novamente.', code='processing')
        stage('error', 'Não foi possível concluir')
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@app.get('/api/jobs/{key}')
async def get_job(key: str):
    prune()
    if key not in jobs:
        saved = history.get(key)
        if saved:
            return {'status':'done','detail':'Transcrição salva','result':saved['result']}
        raise UserError('Esta transcrição expirou. Inicie uma nova transcrição.', 'expired', 404)
    return {k:v for k,v in jobs[key].items() if k != 'updated'}



@app.get('/api/history')
async def list_history(offset: int = 0):
    return history.listing(max(0,offset))


@app.get('/api/history/{key}')
async def read_history(key: str):
    record=history.get(key)
    if not record: raise UserError('Esta transcrição não foi encontrada no histórico.', 'not_found',404)
    return record


@app.post('/api/history/{key}/generate')
async def create_version(key: str, request: Request):
    record=history.get(key)
    if not record: raise UserError('Esta transcrição não foi encontrada no histórico.', 'not_found',404)
    try:
        body=json.loads(await bounded_body(request,8192))
        if not isinstance(body,dict): raise ValueError()
        kind=body.get('kind')
        base_id=body.get('base_id')
        if kind not in editorial.PROMPTS or (base_id is not None and not isinstance(base_id,str)): raise ValueError()
    except (ValueError,TypeError):
        raise UserError('Escolha uma opção válida para gerar o texto.', 'invalid_action')
    if record['result'].get('partial'):
        raise UserError('Esta transcrição está incompleta. Conclua a transcrição antes de criar um roteiro.', 'partial')
    if kind == 'hooks': base_id = None
    source=record['result']['original']
    if base_id:
        base=next((v for v in record['versions'] if v['id']==base_id and v['kind'] in ('copy','script')),None)
        if not base: raise UserError('O texto de origem não foi encontrado nesta transcrição.', 'invalid_source')
        source=base['content']
    if key in creative_busy: raise UserError('Já estamos criando um texto para esta transcrição. Aguarde.', 'busy',409)
    creative_busy.add(key)
    try:
        content=await editorial.generate(kind,source,settings())
        return history.add_version(key,kind,base_id,content)
    except httpx.HTTPError:
        raise UserError('Não foi possível gerar o texto agora. Sua transcrição continua salva. Tente novamente.', 'provider_error',503)
    finally:
        creative_busy.discard(key)


@app.post('/api/downloads')
async def start_download(request: Request):
    try:
        body=json.loads(await bounded_body(request,8192))
        if not isinstance(body,dict) or not isinstance(body.get('url'),str): raise ValueError()
    except ValueError: raise UserError('Cole um link de vídeo válido.', 'invalid_url')
    key,url=downloads.reserve(body['url'])
    task=asyncio.create_task(downloads.run(key,url))
    tasks.add(task);task.add_done_callback(tasks.discard)
    return JSONResponse({'id':key},status_code=202)


@app.get('/api/downloads/{key}')
async def download_status(key: str):
    downloads.cleanup()
    record=downloads.records.get(key)
    if not record: raise UserError('Este download expirou. Cole o link novamente.', 'expired',404)
    return {k:v for k,v in record.items() if k in ('status','detail','size')}


@app.get('/api/downloads/{key}/file')
async def download_file(key: str):
    downloads.cleanup()
    record=downloads.records.get(key)
    if not record or record['status']!='done': raise UserError('O arquivo não está disponível. Inicie o download novamente.', 'unavailable',404)
    record['updated']=time.monotonic()
    return FileResponse(record['file'],filename='video'+record['file'].suffix,media_type='video/mp4')


app.mount('/static' , StaticFiles(directory=ROOT / 'static'), name='static')
