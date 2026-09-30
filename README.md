

# SLA Monitor — Zoho Desk × Zoho Catalyst

Dashboard de inteligência de suporte construído sobre o **Zoho Desk** como BaaS e hospedado no **Zoho Catalyst**. Transforma dados de tickets em alertas preditivos de risco de SLA em tempo real.

---

## Funcionalidades

### Violações de SLA
- Lista todos os tickets abertos com `isOverDue` ou `isResponseOverDue` ativo
- Métricas agregadas: total de infrações, categoria crítica, agente com mais atrasos, prioridade predominante
- Análise de padrões com **Google Gemini** (problemas recorrentes, defeitos, causas de atraso, recomendações)

### Preditor de Risco
- Analisa todos os tickets abertos ainda dentro do prazo
- Calcula score de risco (0–100%) baseado em:
  - **Base:** percentual do SLA consumido × peso da prioridade (Urgent 1.3×, High 1.15×, Medium 1.0×, Low 0.85×)
  - **Boost histórico:** taxa de atraso do agente (60%) + categoria (40%) × até 25 pontos
- Countdown ao vivo em segundos, sem recarregar a página
- Níveis: 🔴 Alto (≥75%) · 🟡 Médio (≥45%) · 🟢 Baixo (<45%)
- Hyperlink direto para o ticket no Zoho Desk

### Data Store (memória histórica)
- **AgentStats:** taxa de atraso por agente + categoria
- **CategoryStats:** taxa de atraso por categoria
- Sincronização via `/sync` (busca os 50 últimos tickets fechados e recalcula as taxas)
- Cache de 30 minutos para evitar leituras excessivas

---

## Arquitetura

```
Zoho Desk API  ──►  Catalyst Function (Python/Flask)  ──►  Catalyst Data Store
                           │
                    Google Gemini API
                           │
                    Catalyst Web Hosting  ──►  Browser (Vanilla JS)
```

- **Backend:** Python 3.13, Flask, `zcatalyst_sdk`, `requests`
- **Frontend:** HTML + CSS + Vanilla JS (sem frameworks)
- **Auth:** OAuth2 server-to-server com cache de token (55 min TTL)
- **IA:** Google Gemini (`gemini-3.6-flash`) com 5 retries e backoff exponencial

---

## Rotas da API

| Método | Rota | Descrição |
|---|---|---|
| GET | `/health` | Status da função |
| GET | `/violations` | Tickets com SLA violado |
| GET | `/metrics` | Métricas agregadas |
| GET | `/predict` | Tickets em risco com scores |
| POST | `/sync` | Sincroniza histórico no Data Store |
| GET | `/stats` | Visualiza dados do Data Store |
| GET | `/debug/datastore` | Debug das tabelas |
| POST | `/analyze` | Análise de padrões com Gemini |

---

## Estrutura do projeto

```
ProjetoDeskxCatalyst/
├── client/
│   ├── index.html
│   └── main.js
├── functions/
│   └── sla_monitor/
│       ├── main.py
│       ├── requirements.txt
│       └── catalyst-config.json   ← nunca commitar
└── .gitignore
```

---

## Configuração

Variáveis de ambiente em `catalyst-config.json` (não commitar):

```json
{
  "deployment": {
    "env_variables": {
      "ZOHO_CLIENT_ID": "...",
      "ZOHO_CLIENT_SECRET": "...",
      "ZOHO_REFRESH_TOKEN": "...",
      "ZOHO_ORG_ID": "...",
      "ZOHO_DESK_DOMAIN": "desk.zoho.com",
      "GEMINI_API_KEY": "..."
    }
  }
}
```

Escopos OAuth necessários: `Desk.tickets.READ Desk.tickets.CREATE Desk.tickets.UPDATE`

---

## Deploy

```bash
catalyst deploy
```

---

## Próximas features planejadas

- Notificações proativas por e-mail/Desk quando score ultrapassa 75%
- Sugestão de resolução baseada em tickets similares fechados
- Redistribuição de carga entre agentes via score de risco
- Cron automático para `/sync` a cada 6h

---

## Repositório

[github.com/RafaelGasparr/sla-monitor-catalyst](https://github.com/RafaelGasparr/sla-monitor-catalyst)
