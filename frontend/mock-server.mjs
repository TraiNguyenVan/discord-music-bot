// Throwaway mock of the aiohttp backend (web/server.py) for UI development.
// Run: WEB_MOCK_PORT=8766 node mock-server.mjs
import { createServer } from 'node:http'

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

const TRACKS = [
  { videoId: 'dQw4w9WgXcQ', title: 'Never Gonna Give You Up', uploader: 'Rick Astley', duration: 213, thumbnail: 'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg' },
  { videoId: '9bZkp7q19f0', title: 'Gangnam Style', uploader: 'officialpsy', duration: 253, thumbnail: 'https://i.ytimg.com/vi/9bZkp7q19f0/hqdefault.jpg' },
  { videoId: 'kJQP7kiw5Fk', title: 'Despacito', uploader: 'Luis Fonsi', duration: 282, thumbnail: 'https://i.ytimg.com/vi/kJQP7kiw5Fk/hqdefault.jpg' },
  { videoId: 'JGwWNGJdvx8', title: 'Shape of You', uploader: 'Ed Sheeran', duration: 263, thumbnail: 'https://i.ytimg.com/vi/JGwWNGJdvx8/hqdefault.jpg' },
  { videoId: 'RgKAFK5djSk', title: 'See You Again', uploader: 'Wiz Khalifa', duration: 237, thumbnail: 'https://i.ytimg.com/vi/RgKAFK5djSk/hqdefault.jpg' },
  { videoId: 'OPf0YbXqDm0', title: 'Uptown Funk', uploader: 'Mark Ronson', duration: 270, thumbnail: 'https://i.ytimg.com/vi/OPf0YbXqDm0/hqdefault.jpg' },
]

const state = {
  playing: true,
  paused: false,
  elapsed: 47,
  duration: 213,
  title: 'Never Gonna Give You Up',
  uploader: 'Rick Astley',
  requester: 'tester',
  connected: true,
  voice_channel: 'General',
  volume: 80,
  loop: 'off',
  autoplay: true,
  queue: [
    { title: 'Gangnam Style', duration: 253, requester: 'alice', qid: 'q-2', uploader: 'officialpsy', index: 2, from_auto: false },
    { title: 'Despacito', duration: 282, requester: 'bob', qid: 'q-3', uploader: 'Luis Fonsi', index: 3, from_auto: false },
    { title: 'Shape of You', duration: 263, requester: 'tester', qid: 'q-4', uploader: 'Ed Sheeran', index: 4, from_auto: false },
    { title: 'See You Again', duration: 237, requester: 'carol', qid: 'q-5', uploader: 'Wiz Khalifa', index: 5, from_auto: false },
    { title: 'Uptown Funk', duration: 270, requester: 'dave', qid: 'q-6', uploader: 'Mark Ronson', index: 6, from_auto: true },
  ],
  listeners: [
    { id: '1', name: 'tester', avatar: 'https://cdn.discordapp.com/embed/avatars/1.png' },
    { id: '2', name: 'alice', avatar: 'https://cdn.discordapp.com/embed/avatars/2.png' },
    { id: '3', name: 'bob', avatar: 'https://cdn.discordapp.com/embed/avatars/3.png' },
    { id: '4', name: 'carol', avatar: 'https://cdn.discordapp.com/embed/avatars/4.png' },
  ],
}

function now() {
  const base = {
    playing: state.playing,
    paused: state.paused,
    queue_len: state.queue.length,
    user_len: state.queue.filter((t) => !t.from_auto).length,
    auto_len: state.queue.filter((t) => t.from_auto).length,
    queue: state.queue.slice(0, 10).map((t) => ({ title: t.title, duration: t.duration, requester: t.requester, qid: t.qid })),
    loop: state.loop,
    volume: state.volume,
    autoplay: state.autoplay,
    auto_status: state.autoplay ? 'mix running' : '',
    connected: state.connected,
    voice_channel: state.voice_channel,
    listeners: state.listeners,
  }
  if (state.playing) {
    return {
      ...base,
      title: state.title,
      uploader: state.uploader,
      requester: state.requester,
      duration: state.duration,
      elapsed: state.elapsed,
      paused: state.paused,
      thumbnail: 'https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg',
      webpage_url: 'https://www.youtube.com/watch?v=dQw4w9WgXcQ',
    }
  }
  return base
}

const json = (body, status = 200) => ({
  status,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

createServer(async (req, res) => {
  const url = new URL(req.url, 'http://x')
  const path = url.pathname
  let body = ''
  for await (const chunk of req) body += chunk

  await sleep(120) // simulate latency

  if (path === '/api/session') {
    res.end(JSON.stringify({ ok: true, guild: 'Music Lounge', user: 'tester', queue_len: state.queue.length, expires_in: null }))
  } else if (path === '/api/config') {
    res.end(JSON.stringify({ ok: true, searchBackends: [], searchInstances: [] }))
  } else if (path === '/api/now') {
    if (state.playing && !state.paused) state.elapsed = Math.min(state.duration, state.elapsed + 2)
    res.end(JSON.stringify({ ok: true, now: now() }))
  } else if (path === '/api/queue') {
    const page = Number(url.searchParams.get('page') || 1)
    res.end(JSON.stringify({
      ok: true,
      current: state.playing ? { index: 1, title: state.title, duration: state.duration, requester: state.requester, uploader: state.uploader, from_auto: false, qid: 'q-1' } : null,
      items: state.queue.map((t, i) => ({ ...t, index: i + 2 })),
      page: 1,
      total_pages: 1,
      total: state.queue.length,
    }))
  } else if (path === '/api/search') {
    const q = url.searchParams.get('q') || ''
    res.end(JSON.stringify({ ok: true, results: TRACKS.filter((t) => t.title.toLowerCase().includes(q.toLowerCase()) || q.length < 3 || true), via: 'mock' }))
  } else if (path === '/api/suggest') {
    const q = url.searchParams.get('q') || ''
    res.end(JSON.stringify({ ok: true, suggestions: TRACKS.slice(0, 3).map((t) => `${q} ${t.title}`.trim()) }))
  } else if (path === '/api/related') {
    res.end(JSON.stringify({ ok: true, results: TRACKS.slice(1, 5), via: 'mock' }))
  } else if (path === '/api/control') {
    const data = JSON.parse(body || '{}')
    const action = data.action
    if (action === 'toggle') state.paused = !state.paused
    if (action === 'skip') {
      const next = state.queue.shift()
      if (next) {
        state.title = next.title
        state.uploader = next.uploader
        state.duration = next.duration
        state.elapsed = 0
      } else {
        state.playing = false
      }
    }
    if (action === 'stop') { state.playing = false; state.elapsed = 0 }
    if (action === 'volume_set') state.volume = Math.max(0, Math.min(200, Number(data.level) || 0))
    if (action === 'volume_delta') state.volume = Math.max(0, Math.min(200, state.volume + (Number(data.delta) || 0)))
    if (action === 'shuffle') state.queue.reverse()
    if (action === 'loop') state.loop = state.loop === 'off' ? 'queue' : state.loop === 'queue' ? 'track' : 'off'
    if (action === 'autoplay') state.autoplay = !state.autoplay
    if (action === 'clear') state.queue = []
    if (action === 'remove' || action === 'jump') {
      const i = state.queue.findIndex((t) => t.qid === data.qid)
      if (i >= 0) {
        if (action === 'jump') { const t = state.queue.splice(i, 1)[0]; state.title = t.title; state.uploader = t.uploader; state.duration = t.duration; state.elapsed = 0; state.playing = true }
        else state.queue.splice(i, 1)
      }
    }
    if (action === 'join') state.connected = true
    if (action === 'leave') { state.connected = false; state.voice_channel = null; state.playing = false }
    res.end(JSON.stringify({ ok: true, message: `**${action}** ok (mock).`, now: now() }))
  } else if (path === '/api/play') {
    res.end(JSON.stringify({ ok: true, message: 'Pick one below (mock).', kind: 'search', results: TRACKS.slice(0, 4) }))
  } else if (path === '/api/pick') {
    const data = JSON.parse(body || '{}')
    state.queue.push({ title: data.title || 'Added track', duration: data.duration || 200, requester: 'tester', qid: `q-${Date.now()}`, uploader: data.uploader || '?', index: 0, from_auto: false })
    res.end(JSON.stringify({ ok: true, message: `**Added** ${data.title || 'track'} to the queue.` }))
  } else {
    res.writeHead(404, { 'Content-Type': 'application/json' })
    res.end(JSON.stringify({ ok: false, error: 'no such route (mock)' }))
  }
}).listen(Number(process.env.WEB_MOCK_PORT) || 8766, () => console.log('mock api on :' + (Number(process.env.WEB_MOCK_PORT) || 8766)))
