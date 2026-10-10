// Screenshot harness via playwright-core + system chromium.
// Usage: node shot.mjs <url> <outfile> [width] [height] [mode]
//   mode: full | viewport | mobile
import { chromium } from 'playwright-core'

const [url, out, w = '1440', h = '900', mode = 'full'] = process.argv.slice(2)
const width = Number(w)
const height = Number(h)
const mobile = mode === 'mobile'

const browser = await chromium.launch({
  executablePath: '/usr/bin/chromium',
  args: ['--no-sandbox', '--disable-gpu', '--hide-scrollbars'],
})
const page = await browser.newPage({
  viewport: { width, height },
  deviceScaleFactor: 1,
  isMobile: mobile,
  hasTouch: mobile,
})
await page.goto(url, { waitUntil: 'networkidle' })
await page.waitForTimeout(1200)
if (mode === 'full') await page.screenshot({ path: out, fullPage: true })
else await page.screenshot({ path: out })
await browser.close()
console.log('saved', out)
