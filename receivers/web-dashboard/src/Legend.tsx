import { STATE_COLORS } from './types'

const LEGEND_ITEMS = [
  { state: 'idle', label: 'Idle', desc: 'Session open, nothing happening', animation: '' },
  { state: 'working', label: 'Working', desc: 'Claude is thinking', animation: 'spin' },
  { state: 'tool_active', label: 'Tool Running', desc: 'Executing a command/tool', animation: 'spin' },
  { state: 'awaiting_input', label: 'Awaiting Input', desc: 'Done — waiting for you', animation: 'bounce' },
  { state: 'awaiting_permission', label: 'Needs Permission', desc: 'Blocked, needs your approval', animation: 'glow' },
  { state: 'notification', label: 'Notification', desc: 'Claude has a message', animation: 'glow' },
  { state: 'error', label: 'Error', desc: 'Something went wrong', animation: 'shake' },
]

export default function Legend() {
  return (
    <div className="legend">
      <div className="legend-title">States</div>
      <div className="legend-items">
        {LEGEND_ITEMS.map(item => (
          <div key={item.state} className="legend-item">
            <span
              className={`legend-dot legend-anim-${item.animation}`}
              style={{ background: STATE_COLORS[item.state] }}
            />
            <div className="legend-text">
              <span className="legend-label">{item.label}</span>
              <span className="legend-desc">{item.desc}</span>
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
