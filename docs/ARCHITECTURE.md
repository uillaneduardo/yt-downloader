# Arquitetura e contrato da API

## Componentes

- UI: HTML/CSS/JS puro, layout monocromático aprovado, responsivo, sem dependências de frontend. Inspiração shadcn/ui visual; **não é React/shadcn real**.
- API: FastAPI/Python com validação Pydantic, retornos JSON padronizados e SSE.
- Executor: fila SQLite, worker único em thread, `yt-dlp` com componentes EJS e Node.js 22 como processo isolado por job, FFmpeg para conversão e metadados.
- Persistência: SQLite WAL em volume Docker; `jobs.kind` e tabela `playlist_items` (migração aditiva automática); arquivos temporários por UUID em `data/jobs`.
- Deploy: 1 container Docker e Cloudflare Tunnel existente. Porta somente loopback.

## Endpoints

| Método | Rota | Uso |
|---|---|---|
| `GET` | `/api/health` | Saúde |
| `POST` | `/api/inspect` | `{ "url": "https://..." }`; retorna `inspectionId`, título, perfil de mídia |
| `POST` | `/api/downloads` | `{ "inspectionId", "profileId", "metadata": true, "publish": false }`; retorna `id`, `token` |
| `GET` | `/api/downloads/{id}?token=...` | Estado do trabalho |
| `GET` | `/api/downloads/{id}/events?token=...` | SSE com estado até conclusão |
| `POST` | `/api/downloads/{id}/cancel?token=...` | Cancelamento |
| `GET` | `/api/downloads/{id}/file?token=...` | Download temporário |
| `GET` | `/api/history` | Registros voluntariamente publicados (máximo 50 por página) |

Erros: `{ "code": "INVALID_URL", "message": "Confira o endereço..." }`, com HTTP apropriado. Detalhes internos ficam no servidor, não são enviados ao cliente.

## Segurança e privacidade

URLs normalizadas apenas de hosts explícitos, IDs de vídeo e IDs de playlist validados. Entrada de playlist é aceita exclusivamente pelo caminho `/playlist?list=...` do YouTube; cada vídeo é reescrito como URL canônica antes de iniciar yt-dlp. Comandos executados sem shell, diretórios aleatórios, limites de duração/tamanho, timeout, limite de trabalhos e requisições. Sem autenticação de usuário, uma chave aleatória de job protege arquivos/status. Tokens na query do SSE e download podem ser registrados por proxies externos: redigir/query-strip dos access logs sempre que possível. Access logs do Uvicorn desabilitados por padrão.

O histórico público só é escrito depois do sucesso **com autorização explícita**, registrando título, URL canônica, plataforma, formato, qualidade e horário. Não registramos identificador do visitante no SQLite. A aplicação calcula um hash transitório de IP na memória para rate limiting; proxies externos podem coletar dados conforme suas próprias políticas.

A inspeção usa subprocess com timeout. Operações de download têm watchdog e são encerradas na falha/cancelamento. No reinício, jobs `running` passam a falha amigável; `queued` permanecem na fila.

## Playlists YouTube

- `POST /api/inspect` retorna `kind=playlist`, `count`, `entries[]` e `profiles[]` em limites configuráveis. A inspeção utiliza `yt-dlp --flat-playlist --playlist-end (MAX_PLAYLIST_ITEMS+1)` e rejeita excesso antes de criar o job.
- `POST /api/downloads` aceita a mesma estrutura do vídeo isolado; gera um job pai `kind=playlist` e linhas em `playlist_items`. A publicação no histórico não é aceita para playlists.
- Worker processa cada item **sincronamente, em ordem**, invocando um subprocesso por vídeo e atualizando `playlist_items` e `jobs`; cancelamento do job pai interrompe o processo corrente. Falha em um item não interrompe os demais.
- `GET /api/downloads/{id}` e SSE incluem objeto `playlist` com `total`, `completed`, `failed` e `items[]` (status/progresso individual).
- Ao concluir, cria-se um ZIP (sem recompressão) com os arquivos numerados. `GET /api/downloads/{id}/file?token=...` retorna esse ZIP; os temporários são excluídos. Falha total devolve `PLAYLIST_FAILED`.
- Limites por item e por agregado: `MAX_FILE_MB`, `MAX_DURATION_SECONDS`, `JOB_TIMEOUT_SECONDS`, `MAX_PLAYLIST_ITEMS` e `MAX_PLAYLIST_TOTAL_MB`. Não há paralelismo entre itens.

## Backlog pós-MVP

- Auditoria de throughput e compatibilidade entre codecs/MP4, especialmente Vimeo.
- Download segmentado para evitar requisições longas em certos limites de proxy.
- Rate limiter compartilhado/Cloudflare Turnstile/WAF e limites por visitante.
- Gestão de disco mais rígida (quota por volume) e métricas operacionais.
- Política de moderação do histórico, remoção e retenção LGPD.
- Melhorias de acessibilidade e E2E real com arquivo pequeno licenciado.

### Limite final e espaço intermediário (v0.2.1)

O yt-dlp e o FFmpeg podem manter simultaneamente vídeo, áudio e arquivo de
conversão. `MAX_FILE_MB` limita somente o arquivo individual entregue; a
soma dos intermediários é monitorada com `MAX_TEMP_MB`. Para playlists,
`MAX_PLAYLIST_TOTAL_MB` limita a soma das saídas e o ZIP final, enquanto
`MAX_PLAYLIST_TEMP_MB` limita a pasta de trabalho incluindo a etapa de ZIP.
O watchdog avalia espaço periodicamente, não oferece quota física estrita.
Recomendamos volume de dados isolado com quota no filesystem para uso público.
