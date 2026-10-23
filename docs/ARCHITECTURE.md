# Arquitetura e contrato da API

## Componentes

- UI: HTML/CSS/JS puro, layout monocromático aprovado, responsivo, sem dependências de frontend. Inspiração shadcn/ui visual; **não é React/shadcn real**.
- API: FastAPI/Python com validação Pydantic, retornos JSON padronizados e SSE.
- Executor: fila SQLite, worker único em thread, `yt-dlp` como processo isolado por job, FFmpeg para conversão e metadados.
- Persistência: SQLite WAL em volume Docker; arquivos temporários por UUID em `data/jobs`.
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

URLs normalizadas apenas de hosts explícitos, IDs de vídeo validados e listas/playlists desconsideradas. Comandos executados sem shell, diretórios aleatórios, limites de duração/tamanho, timeout, limite de trabalhos e requisições. Sem autenticação de usuário, uma chave aleatória de job protege arquivos/status. Tokens na query do SSE e download podem ser registrados por proxies externos: redigir/query-strip dos access logs sempre que possível. Access logs do Uvicorn desabilitados por padrão.

O histórico público só é escrito depois do sucesso **com autorização explícita**, registrando título, URL canônica, plataforma, formato, qualidade e horário. Não registramos identificador do visitante no SQLite. A aplicação calcula um hash transitório de IP na memória para rate limiting; proxies externos podem coletar dados conforme suas próprias políticas.

A inspeção usa subprocess com timeout. Operações de download têm watchdog e são encerradas na falha/cancelamento. No reinício, jobs `running` passam a falha amigável; `queued` permanecem na fila.

## Backlog pós-MVP

- Auditoria de throughput e compatibilidade entre codecs/MP4, especialmente Vimeo.
- Download segmentado para evitar requisições longas em certos limites de proxy.
- Rate limiter compartilhado/Cloudflare Turnstile/WAF e limites por visitante.
- Gestão de disco mais rígida (quota por volume) e métricas operacionais.
- Política de moderação do histórico, remoção e retenção LGPD.
- Melhorias de acessibilidade e E2E real com arquivo pequeno licenciado.
