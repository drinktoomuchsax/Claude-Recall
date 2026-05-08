import { SessionState, StateHistoryEntry, STATE_DISPLAY, EFFORT_COLORS, parseModel } from './types'

interface Props {
  session: SessionState
}

const STATE_CSS_COLOR: Record<string, string> = {
  off: '#555',
  idle: '#4ec970',
  working: '#4a9df8',
  tool_active: '#5bc0de',
  awaiting_input: '#f0ad4e',
  awaiting_permission: '#b07ee8',
  notification: '#c09af0',
  error: '#e85d5d',
}

const TIMELINE_ROW_WIDTH = 40

export default function SessionRow({ session }: Props) {
  const meta = session.metadata
  const modelInfo = parseModel(meta?.model)
  const time = session.lastChange.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
  const stateColor = STATE_CSS_COLOR[session.state] ?? '#555'
  const totalTime = session.durations ? Object.values(session.durations).reduce((s, v) => s + v, 0) : 0
  const toolLine = meta?.tool_name ? formatToolLine(meta.tool_name, meta.tool_context) : null
  const title = meta?.cwd ?? session.id

  const needsYou = ['awaiting_input', 'awaiting_permission'].includes(session.state)
  const hasIssue = ['notification', 'error'].includes(session.state)

  return (
    <div className={`panel ${needsYou ? 'needs-you' : hasIssue ? 'has-issue' : ''}`} style={{ '--sc': stateColor } as React.CSSProperties}>
      {/* Header line: path + right-aligned meta */}
      <div className="p-header">
        <span className="p-title">┌─ {title}</span>
        <span className="p-header-r">
          {modelInfo && <span className="p-model">{modelInfo.short}</span>}
          {meta?.effort_level && meta.effort_level !== 'medium' && (
            <span className="p-effort" style={{ color: EFFORT_COLORS[meta.effort_level] }}>{meta.effort_level}</span>
          )}
          {meta?.agent_type && <span className="p-agent">{meta.agent_type}</span>}
        </span>
      </div>

      {/* Line 1: state + duration info (spread across full width) */}
      <div className="p-line">
        <span className="p-left">
          <span className="p-state" style={{ color: stateColor }}>● {STATE_DISPLAY[session.state]}</span>
          {toolLine && <span className="p-tool">{toolLine}</span>}
        </span>
        <span className="p-right">
          {session.duration != null && session.duration > 0 && (
            <span className="p-dur">↳{fmt(session.duration)}</span>
          )}
          {totalTime > 0 && <span className="p-total">{fmt(totalTime)}</span>}
          <span className="p-time">{time}</span>
        </span>
      </div>

      {/* Line 2: prompt */}
      {meta?.prompt && (
        <div className="p-line">
          <span className="p-prompt" title={meta.prompt}>» {meta.prompt}</span>
        </div>
      )}

      {/* Line 3: error */}
      {meta?.error_type && session.state === 'error' && (
        <div className="p-line">
          <span className="p-error">! {meta.error_type}</span>
        </div>
      )}

      {/* Timeline: heartbeat of state transitions */}
      {session.history.length > 1 && (
        <div className="p-line p-timeline">
          <Timeline history={session.history} />
        </div>
      )}

      {/* Footer border */}
      <div className="p-footer">└{'─'.repeat(60)}</div>
    </div>
  )
}

function Timeline({ history }: { history: StateHistoryEntry[] }) {
  // Split into rows of TIMELINE_ROW_WIDTH, odd rows reversed (S-shape / boustrophedon)
  const rows: StateHistoryEntry[][] = []
  for (let i = 0; i < history.length; i += TIMELINE_ROW_WIDTH) {
    const row = history.slice(i, i + TIMELINE_ROW_WIDTH)
    // Odd rows: reverse direction (snake going back)
    if (rows.length % 2 === 1) row.reverse()
    rows.push(row)
  }

  const lastIdx = history.length - 1

  return (
    <div className="timeline">
      {rows.map((row, ri) => (
        <div key={ri} className={`tl-row ${ri % 2 === 1 ? 'tl-row-rev' : ''}`}>
          {row.map((entry, ci) => {
            const globalIdx = ri % 2 === 1
              ? ri * TIMELINE_ROW_WIDTH + (row.length - 1 - ci)
              : ri * TIMELINE_ROW_WIDTH + ci
            const isLast = globalIdx === lastIdx
            return (
              <span
                key={ci}
                className={`tl-block ${isLast ? 'tl-head' : ''}`}
                style={{ background: STATE_CSS_COLOR[entry.state] ?? '#333' }}
                title={`${STATE_DISPLAY[entry.state] ?? entry.state} @ ${entry.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}`}
              />
            )
          })}
        </div>
      ))}
    </div>
  )
}

function fmt(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60)
    const s = Math.round(seconds % 60)
    return s > 0 ? `${m}m${s}s` : `${m}m`
  }
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  return m > 0 ? `${h}h${m}m` : `${h}h`
}

function formatToolLine(toolName: string, context?: string): string {
  const icons: Record<string, string> = {
    Bash: '$', Edit: '~', Write: '+', Read: '>', Grep: '?', Glob: '*',
    Agent: '@', WebFetch: '↓', WebSearch: '/',
  }
  const icon = icons[toolName] ?? '#'
  if (context) {
    const short = context.length > 45 ? context.slice(0, 42) + '…' : context
    return `${icon} ${short}`
  }
  return `${icon} ${toolName}`
}
