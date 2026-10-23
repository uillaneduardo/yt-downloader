# yt-downloader

Aplicação web para inspecionar vídeos do YouTube/Vimeo e playlists do YouTube, preparando MP4, M4A ou MP3, com capa e metadados quando suportados. Interface monocromática responsiva, sem marca, com acompanhamento real da execução, histórico público **opt-in** e mensagens de erro amigáveis.

## Requisitos

- Docker + Compose (Linux)
- Espaço suficiente em disco para os arquivos temporários
- Cloudflare Tunnel existente ou outro proxy HTTPS

## Início rápido

```bash
git clone https://github.com/uillaneduardo/yt-downloader.git
cd yt-downloader
# Antes do merge: git fetch origin feat/mvp-downloads-homelab && git switch -c homologacao origin/feat/mvp-downloads-homelab
cp .env.example .env
mkdir -p data
sudo chown -R 10001:10001 data
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8098/api/health
```

O Compose publica somente na interface de loopback (`127.0.0.1:8098`), não expondo o serviço diretamente na rede. Para visitantes individuais via Cloudflare Tunnel, ative `TRUST_CF_IP=1` no `.env` **somente** se o túnel for a única entrada HTTP; sem isso, todas as consultas podem compartilhar o mesmo limite de requisições da origem do proxy. No Cloudflare Tunnel, crie um hostname público `yt.ulln.com.br` com serviço `http://localhost:8098` (se `cloudflared` roda na VM). Se o conector Cloudflare roda em container, use endereço e rede compatíveis com essa implantação — `localhost` aponta para o próprio container.

> **Atenção:** esta é a primeira versão para homologação, não um serviço público irrestrito. Há limites de requisições e fila, mas sem CAPTCHA, mitigação avançada de abuso, autenticação administrativa ou monitoramento operacional. Publicar apenas depois de verificar políticas de uso, espaço e tráfego.

## Fluxo

1. `POST /api/inspect` recebe uma URL HTTPS de YouTube/Vimeo ou `https://www.youtube.com/playlist?list=...`, normaliza e inspeciona. Para playlists, devolve os itens e opções de **qualidade máxima solicitada**, sem afirmar que cada vídeo tem a mesma resolução.
2. `POST /api/downloads` recebe ID da inspeção, perfil de formato e opções. A escolha é validada no servidor; entra na fila persistida no SQLite.
3. O worker único executa yt-dlp/FFmpeg em uma pasta isolada por trabalho e acompanha o progresso. **Em playlists, baixa um vídeo de cada vez na ordem**, continua após falhas individuais e, havendo pelo menos um sucesso, entrega os arquivos em ZIP numerados. O navegador mostra o estado individual de cada item.
4. `GET /api/downloads/{id}/events?token=...` acompanha por SSE, com polling de fallback. O token aleatório restringe acesso ao arquivo e ao progresso.
5. A mídia fica disponível por padrão por 2 horas. Limpeza automática remove os arquivos; o histórico opt-in mantém somente dados da mídia e perfil solicitado.

## Configuração

Consulte [docs/DEPLOY.md](docs/DEPLOY.md) para Cloudflare e homelab e [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) para decisões técnicas, endpoints, limitações e segurança.

## Desenvolvimento e testes

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
DATA_DIR=/tmp/yt-downloader-dev uvicorn app.main:app --reload --port 8098
pytest -q
node --check web/app.js
```

Testes usam metadados simulados e não exigem download de mídias externas. O fluxo real de yt-dlp/FFmpeg precisa ser homologado no homelab.

## Limitações

- Vídeos isolados: YouTube/Vimeo. Playlists: YouTube (`/playlist?list=...`), até `MAX_PLAYLIST_ITEMS` vídeos (20 por padrão), somente conteúdo acessível sem autenticação. Transmissões ao vivo, DRM, canais inteiros e bypass não são suportados.
- Em vídeos isolados, as resoluções vêm da inspeção. Em playlists, o usuário escolhe uma **resolução máxima**, e cada item recebe o melhor formato até esse limite; a disponibilidade pode variar. Conversão **não recupera qualidade de áudio**.
- Thumbnail e tags dependem do contêiner de mídia e do suporte do player. Não é garantida compatibilidade universal.
- Sem IP/nome nas tabelas do histórico; logs de Cloudflare, proxy e infraestrutura podem coletar IPs separadamente.
- A plataforma de origem pode mudar e quebrar o yt-dlp. Atualize as imagens periodicamente, testando antes.
- Faça downloads somente de conteúdo que você tem direito de baixar e respeite os termos das plataformas.

## Playlists em sequência

Cole uma URL como `https://www.youtube.com/playlist?list=PL...`. O sistema lista até 20 vídeos, permite escolher **MP4** ou áudio **M4A/MP3** e cria **um trabalho**. Os vídeos são obtidos sequencialmente, com status de cada item e progresso geral por SSE. Itens privados, removidos ou incompatíveis ficam como falha e **não interrompem** os seguintes. Ao final, baixe um **ZIP** com arquivos numerados na ordem da playlist. Se todos falharem, a interface apresenta erro.

O histórico público é **desativado para playlists**: a inspeção em modo plano não comprova se todos os vídeos são públicos, e não publicamos links potencialmente privados ou não listados. O arquivo ZIP também expira após o prazo configurado.

Limites: `MAX_PLAYLIST_ITEMS=20`, `MAX_PLAYLIST_TOTAL_MB=2048` (espaço máximo para conteúdo/ZIP) e `JOB_TIMEOUT_SECONDS=2700` por vídeo. Para evitar sobrecarga em disco, configure o volume de dados com espaço suficiente para os vídeos **e o ZIP temporário**. Playlists enormes são recusadas na inspeção. Um download de ZIP grande pelo Cloudflare Tunnel pode depender dos limites do seu plano/infraestrutura; homologue com dois vídeos pequenos primeiro.


## Limites de armazenamento (v0.2.1)

O arquivo individual final usa `MAX_FILE_MB` (1024 MB por padrão); arquivos temporários
para obtenção, mesclagem e conversão usam `MAX_TEMP_MB` (4096 MB). Esses são limites
diferentes: o uso transitório de múltiplos arquivos não deve causar um falso erro de
“arquivo grande”. Após a conclusão, apenas o arquivo final é retido.

Uma playlist usa `MAX_PLAYLIST_TOTAL_MB` (2048 MB para o ZIP final) e
`MAX_PLAYLIST_TEMP_MB` (6144 MB para a operação temporária). Valores podem ser
ajustados no `.env` do homelab e exigem rebuild/recriação do container. Garanta
espaço livre correspondente no volume `./data`. Mensagens distinguem arquivo final
maior que o permitido, falta de espaço físico e limite de trabalho temporário.

Consulte [docs/DEPLOY.md](docs/DEPLOY.md) para atualização sem limpar os dados.


## Painel administrativo (v0.3)

- URL: `/admin` (ex.: `https://yt.ulln.com.br/admin`).
- Defina `ADMIN_PASSWORD` com pelo menos **16 caracteres** em `.env`, e recrie o container: `sudo docker compose up -d --build`.
- Se estiver vazio, as APIs administrativas permanecem desativadas (HTTP 503).
- Sessão em cookie HttpOnly/SameSite Strict, controle CSRF e limitação de tentativas de login.
- Visão geral: fila, falhas recentes, espaço ocupado e disponível.
- Downloads: títulos, URL normalizada, perfil, situação, tamanho e IP (visível somente ao administrador).
- Arquivos: ocupação por tarefa, remoção apenas de tarefas finalizadas, limpeza de expirados.
- Limites: ajustes persistidos no SQLite, sem reconstruir imagem; entram em vigor imediatamente e podem afetar downloads em andamento.
- IPs começam a ser registrados **após a atualização**. IPs antigos não podem ser recuperados retroativamente. IP retido inicialmente por 7 dias, com valor configurável entre 1 e 30; histórico operacional por 30 dias (7 a 365).
- O histórico público continua exibindo apenas downloads publicados voluntariamente, sem IP.

**Atenção:** IP é dado pessoal. Informe o uso no aviso de privacidade, restrinja `/admin` com Cloudflare Access além do login e evite divulgar os registros. Não ative `TRUST_CF_IP=1` se um visitante puder acessar o serviço diretamente e forjar o cabeçalho. O serviço deve continuar ligado em `127.0.0.1` no host.

**Diagnóstico de limite persistente:** verifique no painel os limites `MAX_FILE_MB` e `MAX_TEMP_MB`, o espaço livre do volume e o erro da execução. Se o arquivo final exceder o limite selecionado, aumentar somente `MAX_TEMP_MB` não resolverá.
