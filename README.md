# yt-downloader

Aplicação web para inspecionar formatos disponíveis em vídeos do YouTube/Vimeo e preparar arquivos MP4, M4A ou MP3, com capa e metadados quando suportados. Interface monocromática responsiva, sem marca, com acompanhamento real da execução, histórico público **opt-in** e mensagens de erro amigáveis.

## Requisitos

- Docker + Compose (Linux)
- Espaço suficiente em disco para os arquivos temporários
- Cloudflare Tunnel existente ou outro proxy HTTPS

## Início rápido

```bash
git clone https://github.com/uillaneduardo/yt-downloader.git
cd yt-downloader
cp .env.example .env
mkdir -p data
sudo chown -R 10001:10001 data
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8098/api/health
```

O Compose publica somente na interface de loopback (`127.0.0.1:8098`), não expondo o serviço diretamente na rede. No Cloudflare Tunnel, crie um hostname público `yt.ulln.com.br` com serviço `http://localhost:8098` (se `cloudflared` roda na VM). Se o conector Cloudflare roda em container, use endereço e rede compatíveis com essa implantação — `localhost` aponta para o próprio container.

> **Atenção:** esta é a primeira versão para homologação, não um serviço público irrestrito. Há limites de requisições e fila, mas sem CAPTCHA, mitigação avançada de abuso, autenticação administrativa ou monitoramento operacional. Publicar apenas depois de verificar políticas de uso, espaço e tráfego.

## Fluxo

1. `POST /api/inspect` recebe uma URL HTTPS de YouTube/Vimeo, normaliza para uma URL canônica, executa inspeção do `yt-dlp` com timeout e devolve os formatos derivados da mídia.
2. `POST /api/downloads` recebe ID da inspeção, perfil de formato e opções. A escolha é validada no servidor; entra na fila persistida no SQLite.
3. O worker único executa yt-dlp/FFmpeg em uma pasta isolada por trabalho e acompanha o progresso real. Mesclagem, conversão e metadados são indicados por etapa (sem inventar porcentagem).
4. `GET /api/downloads/{id}/events?token=...` acompanha por SSE, com polling de fallback. O token aleatório restringe acesso ao arquivo e ao progresso.
5. A mídia fica disponível por padrão por 2 horas. Limpeza automática remove os arquivos; o histórico opt-in mantém somente dados da mídia e perfil solicitado.

## Configuração

Consulte [docs/DEPLOY.md](docs/DEPLOY.md) para Cloudflare e homelab e [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) para decisões técnicas, endpoints, limitações e segurança.

## Desenvolvimento e testes

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt pytest httpx
DATA_DIR=/tmp/yt-downloader-dev uvicorn app.main:app --reload --port 8098
python -m pytest -q
node --check web/app.js
```

Testes usam metadados simulados e não exigem download de mídias externas. O fluxo real de yt-dlp/FFmpeg precisa ser homologado no homelab.

## Limitações

- Apenas URLs públicas normais de YouTube/Vimeo. Links privados/não listados, playlists, transmissões ao vivo, contas autenticadas, DRM e bypass não são suportados.
- Qualidade listada depende dos formatos inspecionados, e uma saída convertida pode ter restrições dos codecs. Conversão **não recupera qualidade de áudio**.
- Thumbnail e tags dependem do contêiner de mídia e do suporte do player. Não é garantida compatibilidade universal.
- Sem IP/nome nas tabelas do histórico; logs de Cloudflare, proxy e infraestrutura podem coletar IPs separadamente.
- A plataforma de origem pode mudar e quebrar o yt-dlp. Atualize as imagens periodicamente, testando antes.
- Faça downloads somente de conteúdo que você tem direito de baixar e respeite os termos das plataformas.
