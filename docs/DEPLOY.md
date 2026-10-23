# Deploy no homelab — `yt.ulln.com.br`

## Instalação (após o merge da PR)

```bash
cd ~/homelab/apps
git clone https://github.com/uillaneduardo/yt-downloader.git
cd yt-downloader
cp .env.example .env
# Ajuste TRUST_CF_IP=1 no .env se cloudflared for o único proxy e a conexão chegar por localhost
mkdir -p data
sudo chown -R 10001:10001 data
sudo docker compose config --quiet
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8098/api/health
```

Se já existe checkout: `git pull --ff-only && sudo docker compose up -d --build --remove-orphans`. Para homologar a PR antes do merge, use `git fetch origin feat/mvp-downloads-homelab && git switch -c homologacao origin/feat/mvp-downloads-homelab` após clone.

## Cloudflare Zero Trust / Tunnel

Utilize o tunnel **já existente** na sua conta Cloudflare (não crie um segundo cloudflared desnecessário). Configure um **Public Hostname**:

- Hostname: `yt.ulln.com.br`
- Type: HTTP
- URL: `localhost:8098` **apenas se cloudflared estiver instalado na VM do homelab**
- Se cloudflared roda em container, substitua pelo hostname de rede Docker ou endereço da VM alcançável por ele. Não use `localhost` apontando para outro container.
- Mantenha HTTPS habilitado no domínio; o serviço local continua HTTP, restrito ao loopback.

Se estiver usando arquivo de configuração local de `cloudflared`, acrescente o hostname antes do catch-all existente:

```yaml
ingress:
  - hostname: yt.ulln.com.br
    service: http://127.0.0.1:8098
  - service: http_status:404
```

**Não substitua as regras existentes pelo exemplo acima.** Adicione apenas a nova rota na configuração atual. Se optar pelo painel Zero Trust, não é necessário editar o YAML local. Evite publicar a porta 8098 no roteador. Limites de download e políticas de tráfego da Cloudflare continuam sujeitos ao plano.

## Operação

```bash
sudo docker compose logs -f --tail=100
sudo docker compose ps
sudo docker compose restart
sudo docker compose down            # mantém ./data
sudo du -sh data
```

Os arquivos são temporários (padrão: 2 horas); o SQLite fica em `./data/app.sqlite3`. `docker compose down -v` e remoção da pasta `data` podem causar perda de histórico e tarefas.

Antes de exposição pública, configurar WAF/Rate Limiting na Cloudflare, revisar regras de uso e limites de disco. **O serviço não protege contra todos os abusos de uma instância pública.**

## Variáveis `.env`

| Variável | Padrão | Significado |
|---|---|---|
| `YT_PORT` | `8098` | Porta local da VM |
| `MAX_FILE_MB` | `1024` | Tamanho máximo do arquivo final individual | 
| `MAX_TEMP_MB` | `4096` | Espaço intermediário por vídeo em MB |
| `MAX_DURATION_SECONDS` | `5400` | Duração limite de vídeo |
| `FILE_TTL_SECONDS` | `7200` | Retenção do arquivo pronto |
| `JOB_TIMEOUT_SECONDS` | `2700` | Tempo limite por processamento |
| `MAX_PLAYLIST_TOTAL_MB` | `2048` | Limite do ZIP final de playlist em MB | 
| `MAX_PLAYLIST_TEMP_MB` | `6144` | Espaço intermediário de playlist em MB | 
| `TRUST_CF_IP` | `0` | Use `1` somente se o tunnel for a única entrada HTTP (porta bindada em loopback no host). O backend valida o IP recebido do cabeçalho; no container, o peer pode ser o gateway Docker |

## Homologação manual

1. URL inválida/domínio de terceiros devem gerar erro amigável.
2. URL autorizada pública deve retornar título, thumbnail, duração e formatos dinâmicos.
3. Escolher MP4 e acompanhar download, conversão e metadados no navegador.
4. Repetir com MP3 e M4A. Conferir tags e capa com player local.
5. Atualizar a página e observar retomada do estado com sessão ativa.
6. Cancelar download na fila e durante execução.
7. Histórico deve ficar vazio sem opt-in e registrar metadados sem identificação do usuário quando habilitado.
8. Aguardar TTL e verificar expiração/remoção do arquivo.
9. Reiniciar container e confirmar restauração de fila e tratamento de jobs interrompidos.

## Habilitar playlists YouTube (v0.2)

Na pasta da aplicação, atualize a branch e faça o rebuild. O banco SQLite existente é migrado automaticamente, sem apagar o histórico.

```bash
cd ~/homelab/apps/yt-downloader
git fetch origin feat/mvp-downloads-homelab
git switch homologacao  # se esta branch já existe na VM
git pull --ff-only origin feat/mvp-downloads-homelab
# Opcional: configure MAX_PLAYLIST_ITEMS=20 e MAX_PLAYLIST_TOTAL_MB=2048 no .env
sudo docker compose config --quiet
sudo docker compose up -d --build
curl -fsS http://127.0.0.1:8098/api/health
```

A aplicação aceita URLs `https://www.youtube.com/playlist?list=...`, limita a 20 itens inicialmente, baixa sequencialmente e entrega um arquivo ZIP. Não publica playlists no histórico. Garanta espaço em disco adicional durante a montagem do ZIP. Se um vídeo individual falhar, os demais continuam. Para homologar, teste uma playlist pública de 2 a 3 vídeos curtos, selecione MP4/MP3, confira a ordem no ZIP e teste cancelamento.

## Solução do erro de tamanho (v0.2.1)

Antes, o backend comparava a soma do vídeo, áudio e arquivos de conversão com
`MAX_FILE_MB`. Isso podia recusar um vídeo final que cabia no limite. Agora,
o limite final é separado dos intermediários (variável `MAX_TEMP_MB`), sem
desativar a proteção de espaço.

Atualização na branch de playlists, mantendo o volume e o banco existentes:

```bash
cd ~/homelab/apps/yt-downloader
git fetch origin
git switch --detach origin/feat/youtube-playlists-sequential
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8098/api/health
```

O health check deve indicar `version: 0.2.1`. Caso continue exibindo o erro,
veja `MAX_FILE_MB` em seu `.env`, confira `df -h .` no servidor e tente
uma resolução inferior. Um arquivo final realmente acima do limite continua
sendo recusado. O volume deve comportar os arquivos intermediários.
