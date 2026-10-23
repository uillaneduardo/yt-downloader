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


### Quotas finais versus temporárias (v0.2.1)

O `yt-dlp` e o `FFmpeg` podem manter simultaneamente arquivos de vídeo, áudio e
conversão antes de gerar a saída. `MAX_FILE_MB` **não** controla a soma desses arquivos:
a validação final usa o tamanho da saída selecionada e `--max-filesize` limita fluxos
de origem quando o extrator fornece tamanhos conhecidos. `MAX_TEMP_MB` protege o
armazenamento intermediário de cada trabalho; `MAX_PLAYLIST_TEMP_MB` monitora a
pasta completa da playlist; `MAX_PLAYLIST_TOTAL_MB` é aplicado ao ZIP final e à
soma das saídas dos itens. Os arquivos intermediários são removidos quando a
operação termina. Os limites não são uma quota rígida do filesystem e o watchdog
verifica periodicamente (aproximadamente a cada segundo). Para serviços públicos,
recomenda-se volume dedicado com quota no nível do sistema de arquivos.


## v0.4.0 — fila individual com limite global

Nova execução de lote: `kind=batch` em `jobs` representa um grupo que **não** é baixável diretamente. Para cada posição escolhida, cria-se uma linha `kind=single` com `parent_id`, `position`, hash do token compartilhado, URL canônica, status e pasta própria. Não é criado arquivo ZIP. O limite global dinâmico `MAX_PARALLEL_DOWNLOADS` é configurado pelo administrador e os workers concorrem atomicamente pela fila SQLite; cada lote também aplica seu limite `concurrency` selecionado pelo usuário. Os status individuais são consultados com o token opaco do pai e os arquivos são servidos apenas se concluídos e não expirados. O grupo pode completar com falhas parciais; seu status agregado reporta resultados.

`POST /api/inspect` reconhece texto com múltiplas URLs; a inspeção das URLs únicas utiliza dois workers de metadados, playlists usam `--flat-playlist` e o limite `MAX_PLAYLIST_ITEMS` impede lotes excessivos. A análise registra avisos para URLs individuais indisponíveis e a UI só lista as válidas. Antes de iniciar, o servidor valida posições únicas, limite total e concorrência. Após reinício, filhos em execução não são retomados, mas os filhos ainda enfileirados continuam; os trabalhos ZIP legados que permaneciam na fila são interrompidos. A antiga rota de arquivo de ZIP fica apenas para arquivos concluídos anteriores até expirarem.