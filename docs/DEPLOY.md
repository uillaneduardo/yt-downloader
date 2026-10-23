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
| `MAX_FILE_MB` | `1024` | Limite do **arquivo final individual**, em MB |
| `MAX_TEMP_MB` | `4096` | Teto de arquivos **intermediários** durante a conversão (por vídeo), em MB |
| `MAX_DURATION_SECONDS` | `5400` | Duração limite de vídeo |
| `FILE_TTL_SECONDS` | `7200` | Retenção do arquivo pronto |
| `JOB_TIMEOUT_SECONDS` | `2700` | Tempo limite por processamento |
| `MAX_PLAYLIST_TOTAL_MB` | `2048` | Limite do ZIP final de uma playlist, em MB |
| `MAX_PLAYLIST_TEMP_MB` | `6144` | Espaço temporário máximo para uma playlist durante processamento/compactação, em MB |
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


## Mensagem “O arquivo excede o tamanho permitido” (v0.2.1)

O limite `MAX_FILE_MB` se aplica **somente ao arquivo final**, não à soma do vídeo,
áudio e arquivos de conversão. Versões anteriores verificavam a soma dos intermediários
e podiam abortar um vídeo válido com um falso erro de tamanho. Esta versão reserva uma
quota separada para esses intermediários (`MAX_TEMP_MB`), mas mantém a verificação
real do arquivo final. Para playlists, também distingue o tamanho do ZIP final
(`MAX_PLAYLIST_TOTAL_MB`) do espaço temporário (`MAX_PLAYLIST_TEMP_MB`).

Para atualizar um checkout que acompanha o PR de playlists:

```bash
cd ~/homelab/apps/yt-downloader
git fetch origin
git switch --detach origin/feat/youtube-playlists-sequential
# O .env existente não é substituído; adicione/ajuste os limites somente se necessário.
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8098/api/health
```

O health check deve retornar `version: 0.2.1`. Para conferência do limite em execução:

```bash
sudo docker compose exec yt-downloader sh -c 'printf "Arquivo=%s MB; Temporário=%s MB\n" "$MAX_FILE_MB" "$MAX_TEMP_MB"'
df -h .
sudo docker compose logs --tail=80 yt-downloader
```

Se o arquivo final realmente ultrapassar `MAX_FILE_MB`, escolha uma qualidade inferior
ou altere o limite no `.env` conscientemente. Não aumente as quotas sem verificar a
capacidade do volume `./data`, pois conversão e montagem de ZIP consomem espaço
adicional. A correção **não garante** que vídeos indisponíveis sejam baixados.


### Habilitar a administração

No `.env`, defina `ADMIN_PASSWORD` (gere com `openssl rand -hex 24`), `IP_RETENTION_DAYS=7`, `JOB_RETENTION_DAYS=30` e mantenha `TRUST_CF_IP=1` **apenas** quando a conexão direta estiver bloqueada e a única entrada for o túnel Cloudflare.

```bash
cd ~/homelab/apps/yt-downloader
grep -q '^ADMIN_PASSWORD=' .env || printf 'ADMIN_PASSWORD=%s\n' "$(openssl rand -hex 24)" >> .env
sudo docker compose up -d --build
curl -fsS http://127.0.0.1:8098/api/health
```

Abra `https://yt.ulln.com.br/admin`. A senha fica no `.env` do servidor; não a inclua no repositório. Configure Cloudflare Access para restringir `/admin` e, preferencialmente, `/api/admin/*`. A interface usa cookies HttpOnly e tokens CSRF.


## v0.4.0 — lotes individuais e execução paralela

Após o deploy pelo procedimento principal, em **Admin > Limites** ajuste **Downloads simultâneos** (global). Na página pública, o visitante pode informar várias URLs separadas por vírgula, `;`, espaço ou Enter; expanda uma playlist, selecione apenas os itens desejados e escolha X de 1 até o limite permitido. Cada arquivo finalizado terá botão **Baixar** na própria lista. **Não há ZIP para novos lotes.**

O Compose passa `MAX_PARALLEL_DOWNLOADS=2` como padrão para instalações novas; se você já salvou o limite pelo painel, o SQLite prevalece. Não apague a pasta `data/`. Para validar, escolha duas mídias pequenas de uso autorizado e concorrência 2, e verifique que os dois subprocessos são executados sem exceder a capacidade do homelab. A integração externa de download, conversão e Cloudflare Tunnel exige teste na VM.
### Downloads individuais pelo administrador

Na área **Arquivos armazenados**, use **Baixar** para transferir diretamente do servidor cada arquivo de um trabalho concluído, ou **Excluir** para removê-lo. A transferência passa por `/api/admin/jobs/{job_id}/file`, exige login e não usa link público de visitante. Os arquivos expirados continuam sujeitos à limpeza automática.
