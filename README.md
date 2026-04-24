# MicroFlip

Buy and sell micro-SaaS projects from $500 to $50k — verified revenue.

## How to run

```bash
pip install -r requirements.txt
cp .env.example .env   # edit with your credentials
python app.py
```

Open http://localhost:5000

## Features

- **Auth** — Sign up and log in with email + password, secure hash via werkzeug, session cookies
- **Listings** — Create, edit, and browse listings with title, description, URL, asking price, MRR, stack, screenshot upload
- **Stripe Verify** — Connect Stripe via OAuth (read-only) to verify real MRR; listings get a "Revenue Verified" badge
- **Browse & Search** — Public directory with filters by price range, MRR range, stack, and sort by recency/price/MRR
- **Contact Intent** — Logged-in buyers click "I'm Interested"; seller receives email with buyer's profile and message
- **Dashboard** — View your listings, verify MRR status, and see all received interest with buyer contact info

## Stack

- Python 3 + Flask
- SQLite
- htmx + Tailwind CSS (via CDN)

## Next steps

- Escrow payments via Stripe for secure transactions
- In-app messaging thread between buyer and seller
- Upload screenshots to S3/Cloudflare R2 instead of local disk
- Post-sale rating and review system
- Admin panel for moderation
- Deploy with Docker + Gunicorn
