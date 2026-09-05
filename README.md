# TuneYa Store — company website (API review ready)

Static bilingual site for **Shenzhen Colorful Youjia E-commerce Co., Ltd.** / TuneYa Store.

## Pages

| Path | Purpose |
|------|---------|
| `/` | Home + API data-use statement |
| `/services.html` | Tracking & POD scope |
| `/about.html` | Legal company details |
| `/privacy.html` | Privacy Policy |
| `/terms.html` | Terms of Service |

## Deploy to Vercel (your steps)

1. Create an empty GitHub repo (e.g. `tuneyastore-website`).
2. In this folder, run:

```bash
git init
git add .
git commit -m "Add TuneYa Store company website for carrier API review"
git branch -M main
git remote add origin https://github.com/<YOUR_USER>/<YOUR_REPO>.git
git push -u origin main
```

3. In [Vercel](https://vercel.com): **Add New Project** → Import the GitHub repo.
4. Framework Preset: **Other** (static). Root directory: `.` — no build command needed.
5. Deploy, then in Vercel → **Domains** → add `www.tuneyastore.com` and `tuneyastore.com`.
6. At your DNS provider, add the records Vercel shows (usually CNAME `www` → `cname.vercel-dns.com`, and A/`@` as instructed).
7. Wait for HTTPS to become Active, then open:
   - https://www.tuneyastore.com/
   - https://www.tuneyastore.com/privacy.html
   - https://www.tuneyastore.com/about.html

## Contact used on site

- Email: support@tuneyastore.com
- Phone: +86 150 8335 3651
