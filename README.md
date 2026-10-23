# yt-downloader

Downloader web auto-hospedado de vídeo e áudio (YouTube / Vimeo), com interface monocromática, fila persistente, administração, metadados, histórico público opcional e arquivos individuais.

## Recursos v0.5.0

- Cole **um ou vários links**, separados por vírgula, ponto e vírgula, espaços, tabulações ou linhas (Enter).
- Aceita URLs de vídeo YouTube/Vimeo e playlists do YouTube. Playlists são expandidas para uma lista de vídeos individuais.
- Cada vídeo aparece em um card próprio, com seleção de vídeo/áudio, formato e qualidade, botão Preparar arquivo, progresso e Baixar arquivo.
- Preparar selecionados mantém o processamento coletivo e a concorrência definida pelo visitante.
- **Nenhum ZIP é criado para novos lotes**: cada item concluído apresenta seu botão **Baixar** próprio na lista.
- Escolha **X downloads simultâneos** (de 1 até o teto global do administrador, 2 por padrão, máximo configurável de 8).
- A fila respeita o máximo global mesmo com vários visitantes. Um erro individual não cancela os demais.
- Progresso via SSE (com polling de fallback), cancelamento do lote e acesso a cada arquivo por token temporário.
- Painel `/admin` para limites em tempo real, auditoria de links/IPs e limpeza de temporários. IP retido conforme prazo configurado; histórico público sem IP.

**IMPORTANTE:** A aplicação faz download no servidor antes de disponibilizar o arquivo individual ao navegador, e arquivos expiram após o TTL definido em `FILE_TTL_SECONDS`. Quem quiser manter os arquivos deve baixar cada um antes de expirar. O download do navegador é individual; não é um botão para baixar todos ao mesmo tempo.

## Implantação no homelab

```bash
cd ~/homelab/apps/yt-downloader
git fetch origin
git switch main && git pull --ff-only origin main
sudo docker compose up -d --build
sudo docker compose ps
curl -fsS http://127.0.0.1:8098/api/health
```

Esta versão é instalada diretamente da main após o merge. Preserve data/ e .env. Não exclua `data/` nem sobrescreva seu `.env`.

Para uma instalação nova, copie `.env.example` para `.env`, configure `ADMIN_PASSWORD` com pelo menos 16 caracteres e use `sudo chown -R 10001:10001 data` antes de iniciar. O serviço permanece em `127.0.0.1:${YT_PORT:-8098}` para uso pelo Cloudflare Tunnel, domínio `yt.ulln.com.br`.

## Configuração

- `MAX_PARALLEL_DOWNLOADS=2`: máximo global de download/conversão em paralelo; ajustável em `/admin` (1–8), persistido no SQLite.
- `MAX_PLAYLIST_ITEMS=20`: máximo de links/vídeos permitidos por análise; ajustável em `/admin` (1–50).
- `MAX_FILE_MB=1024`: tamanho máximo do arquivo individual final; ajustável em `/admin`.
- `MAX_TEMP_MB=4096`: armazenamento temporário máximo durante o processamento de **cada** item; ajustável em `/admin`.
- `FILE_TTL_SECONDS=7200`: expiração individual após conclusão.
- `JOB_TIMEOUT_SECONDS=2700`: duração máxima de processamento por vídeo.

`MAX_PLAYLIST_TOTAL_MB` e `MAX_PLAYLIST_TEMP_MB` são **somente legados** para lotes antigos criados antes da atualização, que usavam ZIP. Não são usados para novos lotes.

Um lote de 4 vídeos com concorrência 2 processa até 2 itens ao mesmo tempo; um segundo visitante compartilha o mesmo teto global, não recebe outros 2 independentemente. O administrador pode reduzir o teto sem reiniciar os workers.

## API

| Método | Rota | Função |
|---|---|---|
| POST | `/api/inspect` | Analisa URL única, playlist ou texto com vários links |
| POST | `/api/downloads` | Cria trabalho único ou lote com `selectedPositions` e `concurrency` |
| GET | `/api/downloads/{id}?token=...` | Status do trabalho/lote e itens individuais |
| GET | `/api/downloads/{id}/events?token=...` | SSE |
| GET | `/api/downloads/{childId}/file?token={parentToken}` | Download direto de um item concluído |
| POST | `/api/downloads/{id}/cancel?token=...` | Cancela item/lote |
| GET | `/api/capabilities` | Teto global e limite de itens |
| GET | `/api/history` | Histórico opt-in sem IP |

Exemplo de requisição de lote (use um `inspectionId` válido):

```json
{"inspectionId":"id-da-inspecao","profileId":"video-mp4-720","selectedPositions":[1,3],"concurrency":2,"metadata":true,"publish":false}
```

O backend rejeita posições inválidas, duplicadas ou concorrência acima do teto. Cada child job tem sua própria pasta, expiração e status, mas exige o **token secreto do lote** para transferência.

## Segurança e limitações

Não existem URLs arbitrárias: somente domínios e estruturas suportadas são aceitos, sem execução via shell. O serviço possui limite de requisições e de fila. Não divulgue tokens de download: seus links podem aparecer em logs de terceiros. Configure Cloudflare Access para `/admin` e `/api/admin/*`, WAF e limites de taxa antes de expor o serviço.

Playlists são listadas em modo plano: a resolução informada é um teto e a qualidade real pode variar de vídeo para vídeo. Alguns itens podem estar privados, indisponíveis, protegidos ou apresentar falhas; os demais continuam. A inspeção com muitos links pode levar tempo. Transmissões ao vivo, DRM e bypass de permissões não são suportados.

O limite de concorrência não substitui a quota de disco do host. Baixe somente conteúdo que você tenha permissão para usar. Download real em YouTube/Vimeo e funcionamento do Cloudflare Tunnel ainda exigem homologação no homelab.

Veja [docs/DEPLOY.md](docs/DEPLOY.md), [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) e os testes em `tests/test_api.py`.
## Arquivos no painel de administração

Em **/admin > Arquivos armazenados**, cada trabalho concluído oferece **Baixar** (arquivo original preparado) e **Excluir**. A rota `GET /api/admin/jobs/{id}/file` exige sessão administrativa. Ela nunca aceita caminho fornecido pelo usuário e só serve arquivos já concluídos, disponíveis no armazenamento.

## v0.5.0 — cards individuais para listas e playlists

Após analisar vários links, o frontend mostra todos os vídeos em cards com controles independentes de formato, qualidade e Preparar arquivo. Cada arquivo possui status e botão Baixar próprio; não gera ZIP. O visitante ainda pode marcar vários itens e usar Preparar selecionados, com concorrência limitada pelo servidor.

Para links avulsos, o yt-dlp fornece perfis de cada vídeo, exibidos individualmente. A inspeção plana de playlists oferece qualidades máximas (Até Xp), sem garantir que cada vídeo possua a resolução. O endpoint POST /api/downloads aceita `itemProfiles` opcional, mapeando posições (1-based) para IDs dos perfis, por exemplo `{"1":"video-mp4-720","2":"audio-mp3-192"}`. Os perfis são verificados por vídeo; o campo legado `profileId` permanece suportado. O token temporário continua necessário para cada arquivo baixado.
