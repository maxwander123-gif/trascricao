import httpx
from core import text_json, UserError

PROMPTS = {
 'copy': 'Adapte o conteúdo em uma copy fluida e natural em português brasileiro, pronta para falar em vídeo. Localize expressões idiomáticas, referências linguísticas e ritmo, principalmente quando a fonte estiver em inglês. Preserve a mensagem, fatos, nomes, números e intenção. Não faça tradução literal e não invente resultados, provas, depoimentos, ofertas ou promessas. Não acrescente chamada comercial que a fonte não sustente. Entregue a copy completa em parágrafos, sem comentários sobre o processo.',
 'script': 'Reescreva o conteúdo como um roteiro de vídeo em português brasileiro, com frases naturais para falar, uma abertura clara e transições fluidas. Varie a construção das frases e a ordem quando isso melhorar a narrativa. Preserve as informações, nomes, números e intenção. Não invente fatos, resultados, depoimentos, promessas ou ofertas. Entregue o roteiro completo em parágrafos, sem resumo e sem comentários sobre o processo.',
 'hooks': 'Crie exatamente 10 ganchos diferentes em português brasileiro para a abertura de um vídeo baseado no conteúdo recebido. Cada gancho deve ter uma ou duas frases curtas, soar natural e ser fiel ao assunto. Varie abordagem, curiosidade, pergunta e ponto de vista, sem inventar números, resultados, promessas ou fatos. Não repita o mesmo gancho com pequenas trocas de palavras.'
}


async def generate(kind, source, config):
    if kind not in PROMPTS: raise UserError('Escolha uma das opções de criação disponíveis.', 'invalid_action')
    if not config.get('OPENAI_API_KEY'): raise UserError('Configure sua chave da OpenAI para gerar este texto.', 'not_configured',503)
    if kind == 'hooks':
        schema={'type':'object','properties':{'hooks':{'type':'array','items':{'type':'string'},'minItems':10,'maxItems':10}},'required':['hooks'],'additionalProperties':False}
    else:
        schema={'type':'object','properties':{'text':{'type':'string'}},'required':['text'],'additionalProperties':False}
    async with httpx.AsyncClient(headers={'Authorization':'Bearer '+config['OPENAI_API_KEY']},timeout=httpx.Timeout(180,connect=20),trust_env=False) as client:
        data = await text_json(client,config,PROMPTS[kind],source,schema)
    if kind == 'hooks':
        hooks=data.get('hooks',[])
        if len(hooks)!=10 or any(not isinstance(h,str) or not h.strip() for h in hooks) or len(set(h.strip().casefold() for h in hooks)) != 10:
            raise UserError('Não foi possível gerar 10 ganchos distintos. Tente novamente.', 'incomplete')
        return '\n\n'.join(f'{i+1}. {h.strip()}' for i,h in enumerate(hooks))
    text=data.get('text','').strip()
    if not text: raise UserError('O texto não foi concluído. Tente novamente.', 'incomplete')
    return text
