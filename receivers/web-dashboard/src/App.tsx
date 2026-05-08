import { useRecall } from './useRecall'
import ShopWindow from './ShopWindow'
import Legend from './Legend'
import { STATE_COLORS, STATE_DISPLAY } from './types'

function App() {
  const { sessions, aggregate, connected } = useRecall()
  const sessionList = Object.values(sessions)
  const aggColor = STATE_COLORS[aggregate.state] ?? '#333'

  return (
    <div className="app">
      <header className="header">
        <h1>Claude Recall</h1>
        <div className={`connection ${connected ? 'on' : 'off'}`}>
          {connected ? 'Live' : 'Reconnecting...'}
        </div>
      </header>

      {/* Street view: aggregate neon sign */}
      <div className="street-sign" style={{ '--glow': aggColor } as React.CSSProperties}>
        <div className="sign-text">
          {STATE_DISPLAY[aggregate.state] ?? aggregate.state}
        </div>
        <div className="sign-sub">
          {aggregate.activeSessions === 0
            ? 'All shops closed'
            : `${aggregate.activeSessions} shop${aggregate.activeSessions > 1 ? 's' : ''} open`}
        </div>
      </div>

      {/* Legend */}
      <Legend />

      {/* Shop windows row */}
      <div className="shop-street">
        {sessionList.length === 0 ? (
          <div className="empty-street">
            <p>The street is quiet...</p>
            <p className="hint">Start a Claude Code session to see shops appear</p>
          </div>
        ) : (
          sessionList.map((session, i) => (
            <ShopWindow key={session.id} session={session} themeIndex={i} />
          ))
        )}
      </div>
    </div>
  )
}

export default App
