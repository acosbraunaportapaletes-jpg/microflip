# MicroFlip

Compre e venda micro-SaaS de $500 a $50k com métricas de receita verificadas via Stripe.

## Como rodar

```bash
pip install -r requirements.txt
cp .env.example .env   # edite com suas credenciais
python app.py
```

Acesse http://localhost:5000

## Features implementadas

- **Auth** — Cadastro e login com email+senha, hash seguro via werkzeug, sessão via cookie
- **Listings** — Criação, edição e browse de listings com screenshot upload, status draft/active/sold
- **Stripe Verify** — Integração OAuth com Stripe para verificar MRR real dos últimos 6 meses (badge "MRR Verificado")
- **Browse & Search** — Página pública com filtros por faixa de preço e MRR, ordenação por recente/preço, busca por texto
- **Ofertas** — Buyer envia oferta com valor + mensagem; seller aceita/rejeita no painel
- **Chat** — Mensagens em tempo real (htmx) entre buyer e seller após match de oferta
- **Dashboard** — Painel unificado com meus listings, ofertas recebidas e enviadas

## Stack

- Python 3 + Flask
- SQLite
- htmx + Tailwind CSS (via CDN)

## Próximos passos

- Notificação por email quando oferta é recebida (SMTP já previsto nas env vars)
- Integração de pagamento/escrow via Stripe para transações seguras
- Upload de screenshots para S3/Cloudflare R2 em vez de disco local
- Sistema de avaliação/rating pós-venda
- API REST para integrações externas
- Deploy com Docker + Gunicorn
