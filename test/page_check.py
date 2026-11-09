# Headless check: load the page, watch 60s, assert no blank/undefined/NaN/broken-image cards; screenshot.
import sys, time, json
from playwright.sync_api import sync_playwright
base = sys.argv[1] if len(sys.argv) > 1 else 'http://localhost:8899'
secs = int(sys.argv[2]) if len(sys.argv) > 2 else 60
out = sys.argv[3] if len(sys.argv) > 3 else 'page.png'
CHECK = """() => { const bad = []; const cards = [...document.querySelectorAll('.card')];
 for (const c of cards) { const t = c.innerText; const n = c.querySelector('.name')?.innerText.trim();
  if (!n) bad.push(['blank-name', c.dataset.id]);
  if (/\\bundefined\\b|\\bNaN\\b|\\bnull\\b|\\[object/.test(t)) bad.push(['bad-text', c.dataset.id, t.slice(0, 120)]);
  const img = c.querySelector('img'); if (!img || (img.complete && img.naturalWidth === 0)) bad.push(['broken-img', c.dataset.id, img && img.src]); }
 return { cards: cards.length, bad, sources: [...new Set(cards.map(c => c.querySelector('.badge')?.innerText))] }; }"""
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page(viewport={'width': 1400, 'height': 1600}); errs = []
    pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(base + '/', wait_until='networkidle'); fails = []; last = None
    t0 = time.time()
    while time.time() - t0 < secs:
        time.sleep(5); pg.wait_for_timeout(1500)  # let lazy images settle
        r = pg.evaluate(CHECK); last = r; fails += r['bad']
    for tab in ['pump','stonk','raydium','fomo-sol','fills','agents','migrations','fomo-rh','all']:
        pg.click(f'[data-tab="{tab}"]'); pg.wait_for_timeout(1200); fails += pg.evaluate(CHECK)['bad']
    pg.screenshot(path=out, full_page=False)
    lp = b.new_page(); lp.goto(base + '/listen', wait_until='domcontentloaded'); lp.wait_for_timeout(4000); listen_ok = lp.title() != '' and lp.locator('body').inner_text().strip() != ''
    lp.screenshot(path=out.replace('.png', '-listen.png'))
    b.close()
uniq = {json.dumps(f[:2]) for f in fails}
print(json.dumps({'cards': last['cards'], 'sources': last['sources'], 'failures': len(uniq), 'sample': list(uniq)[:8], 'pageErrors': errs[:5], 'listenOk': listen_ok}))
sys.exit(0 if not uniq and not errs and last['cards'] > 0 and listen_ok else 1)
