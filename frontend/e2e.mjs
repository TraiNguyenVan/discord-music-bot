// End-to-end interaction pass against the mock API + screenshots.
import { chromium } from 'playwright-core'

const browser = await chromium.launch({ executablePath: '/usr/bin/chromium', args: ['--no-sandbox'] })
const errors = []

// ---------- desktop: search → preview → add ----------
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
page.on('pageerror', (e) => errors.push('pageerror: ' + e.message))
page.on('console', (m) => m.type() === 'error' && errors.push('console: ' + m.text()))
await page.goto('http://localhost:5199/?token=mock', { waitUntil: 'networkidle' })
await page.waitForTimeout(1500)

// search
await page.fill('#q', 'shape')
await page.click('button:has-text("Search")')
await page.waitForTimeout(800)
const rows = await page.locator('.rowwrap').count()
console.log('search rows:', rows)

// preview a result (starts muted audition → related section appears)
await page.locator('.rowwrap .row').first().click()
await page.waitForTimeout(2500)
console.log('audition on:', await page.locator('.aud-slot.on').count() === 1)
console.log('related section:', await page.locator('section[aria-label="Related tracks"]').count() === 1)

// add to queue from results
await page.locator('.rowwrap .radd').first().click()
await page.waitForTimeout(900)
const toast = await page.locator('.toast .readout').textContent().catch(() => null)
console.log('toast after add:', toast)

// volume slider drag → volume_set posts
await page.locator('.pbar input[type=range]').evaluate((el) => {
  el.value = '120'
  el.dispatchEvent(new Event('input', { bubbles: true }))
  el.dispatchEvent(new Event('change', { bubbles: true }))
})
await page.waitForTimeout(600)
console.log('volume label:', await page.locator('.pbar .pct').textContent())

// skip
await page.click('.pbar [aria-label="Skip to the next track"]')
await page.waitForTimeout(1200)
console.log('title after skip:', await page.locator('.np-title').textContent())
await page.screenshot({ path: 'shots/final-desktop.png' })

// sidebar nav jump
await page.click('.snav a[href="#queue"]')
await page.waitForTimeout(700)
const qTop = await page.evaluate(() => document.querySelector('.queuecol').getBoundingClientRect().top)
console.log('queue panel top after nav jump (sticky ≈ 68):', Math.round(qTop))

// ---------- mobile: compact bar + queue sheet ----------
const mob = await browser.newPage({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true })
mob.on('pageerror', (e) => errors.push('mobile pageerror: ' + e.message))
await mob.goto('http://localhost:5199/?token=mock', { waitUntil: 'networkidle' })
await mob.waitForTimeout(1500)

await mob.screenshot({ path: 'shots/final-mobile.png' })

// open queue sheet from the bar
await mob.click('.pbar [aria-label^="Open the queue"]')
await mob.waitForTimeout(600)
const sheetVisible = await mob.evaluate(() => {
  const el = document.querySelector('.queuecol')
  const cs = getComputedStyle(el)
  const b = el.getBoundingClientRect()
  return { pos: cs.position, visibility: cs.visibility, y: Math.round(b.y) }
})
console.log('mobile queue sheet:', JSON.stringify(sheetVisible))
await mob.screenshot({ path: 'shots/final-mobile-sheet.png' })

// close via the visible scrim strip above the sheet
await mob.click('.qscrim', { position: { x: 195, y: 100 } })
await mob.waitForTimeout(500)
const closed = await mob.evaluate(() => document.body.classList.contains('qsheet'))
console.log('sheet closed after scrim tap:', !closed)

// reopen and close with Escape (keyboard path)
await mob.click('.pbar [aria-label^="Open the queue"]')
await mob.waitForTimeout(500)
await mob.keyboard.press('Escape')
await mob.waitForTimeout(500)
const closedByKey = await mob.evaluate(() => document.body.classList.contains('qsheet'))
console.log('sheet closed after Escape:', !closedByKey)

console.log('errors:', errors.length ? errors : 'none')
await browser.close()
