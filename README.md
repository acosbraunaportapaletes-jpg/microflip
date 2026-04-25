# MicroFlip

Buy and sell micro-SaaS projects from $500 to $50k with verified revenue.

## How to run

```bash
pip install -r requirements.txt
cp .env.example .env   # edit with your credentials
python app.py
```

Open http://localhost:5000

## Features

- **Auth** — Register and login with email + password + role (buyer/seller/both), secure hash via werkzeug, session cookies
- **Listings** — Sellers create listings with title, URL, description, asking price, declared MRR, stack tags, and screenshot URL
- **Revenue Verify** — Seller connects Stripe via OAuth read-only; system pulls real MRR from last 6 months and displays a "Revenue Verified" badge
- **Browse & Search** — Public feed with filters by price range, MRR, stack, verified status, and sort by recency/price/MRR
- **Offer Flow** — Logged-in buyer sends offer (amount + message); seller accepts, rejects, or counter-offers via private thread
- **Dashboard** — Seller sees their listings + received offers; Buyer sees sent offers with status tracking

## Stack

- Python 3 + Flask
- SQLite
- htmx + Tailwind CSS (via CDN)

## Next steps

- Escrow payments via Stripe for secure transactions
- In-app real-time messaging between buyer and seller
- File uploads to S3/Cloudflare R2 for screenshots
- Email notifications on new offers and status changes
- Admin panel for moderation
- Deploy with Docker + Gunicorn
