// gamestat-tui — a ratatui terminal UI for gamestat.
//
// It shells out to the `gamestat` Python tool (the tested backend): `--json`
// for data and `uninstall <uid> --yes` for removal. Set GAMESTAT_BIN to point
// at a specific gamestat executable (default: "gamestat" on PATH).

use std::io::Write;
use std::process::Command;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use ratatui::backend::TestBackend;
use ratatui::crossterm::event::{self, Event, KeyCode, KeyEventKind};
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style, Stylize};
use ratatui::text::{Line, Span};
use ratatui::widgets::{
    Block, BorderType, Cell, Clear, Paragraph, Row, Table, TableState, Wrap,
};
use ratatui::{Frame, Terminal};
use serde::Deserialize;

// ---------------------------------------------------------------------------
// Data model (subset of `gamestat --json`)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Deserialize)]
#[allow(dead_code)] // some fields are part of the JSON contract but unused in the TUI
struct Game {
    uid: String,
    source: String,
    name: String,
    #[serde(default)]
    size: u64,
    #[serde(default)]
    playtime: u64, // minutes
    #[serde(default)]
    has_playtime: bool,
    #[serde(default)]
    last_played: i64, // unix seconds
    #[serde(default)]
    cloud: bool,
    #[serde(default)]
    appid: u64,
    #[serde(default)]
    tool: bool,
}

#[derive(Debug, Deserialize)]
struct Scan {
    games: Vec<Game>,
}

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------

fn fmt_size(b: u64) -> String {
    let units = ["B", "KB", "MB", "GB", "TB"];
    let mut v = b as f64;
    let mut i = 0;
    while v >= 1024.0 && i < 4 {
        v /= 1024.0;
        i += 1;
    }
    if i > 0 && v < 10.0 {
        format!("{v:.1} {}", units[i])
    } else {
        format!("{v:.0} {}", units[i])
    }
}

fn fmt_time(min: u64, has: bool) -> String {
    if !has {
        return "—".into();
    }
    if min == 0 {
        return "—".into();
    }
    let h = min as f64 / 60.0;
    if h < 1.0 {
        format!("{min}m")
    } else if h < 100.0 {
        format!("{h:.1}h")
    } else {
        format!("{:.0}h", h)
    }
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

fn fmt_date(t: i64) -> String {
    if t == 0 {
        return "never".into();
    }
    let d = (now_secs() - t) as f64 / 86400.0;
    if d < 1.0 {
        "today".into()
    } else if d < 2.0 {
        "yesterday".into()
    } else if d < 30.0 {
        format!("{}d ago", d.round() as i64)
    } else if d < 365.0 {
        format!("{}mo ago", (d / 30.0).round() as i64)
    } else {
        format!("{}y ago", (d / 365.0).round() as i64)
    }
}

fn source_color(s: &str) -> Color {
    match s {
        "Steam" => Color::Rgb(102, 192, 244),
        "Epic" => Color::Rgb(213, 128, 255),
        "GOG" => Color::Rgb(162, 89, 255),
        "Amazon" => Color::Rgb(255, 153, 0),
        "Lutris" => Color::Rgb(255, 107, 26),
        "Battle.net" => Color::Rgb(0, 168, 255),
        "Riot" => Color::Rgb(255, 70, 85),
        "EA" => Color::Rgb(255, 92, 92),
        "Ubisoft" => Color::Rgb(58, 155, 255),
        _ => Color::Rgb(122, 162, 200),
    }
}

// ---------------------------------------------------------------------------
// Backend calls
// ---------------------------------------------------------------------------

fn gamestat_bin() -> String {
    std::env::var("GAMESTAT_BIN").unwrap_or_else(|_| "gamestat".into())
}

fn load_games() -> Result<Vec<Game>, String> {
    let out = Command::new(gamestat_bin())
        .arg("--json")
        .output()
        .map_err(|e| format!("failed to run `{}`: {e}", gamestat_bin()))?;
    if !out.status.success() {
        return Err(format!(
            "gamestat --json failed: {}",
            String::from_utf8_lossy(&out.stderr)
        ));
    }
    let scan: Scan =
        serde_json::from_slice(&out.stdout).map_err(|e| format!("bad JSON from gamestat: {e}"))?;
    Ok(scan.games)
}

/// Returns Ok(message) on success, Err(message) on failure.
fn run_uninstall(uid: &str) -> Result<String, String> {
    let out = Command::new(gamestat_bin())
        .args(["uninstall", uid, "--yes"])
        .output()
        .map_err(|e| format!("failed to run gamestat uninstall: {e}"))?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    let stderr = String::from_utf8_lossy(&out.stderr);
    if out.status.success() {
        // surface the "Done. Freed X." line if present
        let msg = stdout
            .lines()
            .rev()
            .find(|l| l.trim_start().starts_with("Done."))
            .unwrap_or("Uninstalled.")
            .trim()
            .to_string();
        Ok(msg)
    } else {
        let msg = stderr
            .lines()
            .chain(stdout.lines())
            .map(|l| l.trim())
            .find(|l| !l.is_empty())
            .unwrap_or("uninstall failed")
            .to_string();
        Err(msg)
    }
}

// ---------------------------------------------------------------------------
// App state
// ---------------------------------------------------------------------------

#[derive(Clone, Copy, PartialEq)]
enum Sort {
    Playtime,
    Size,
    Recent,
    Name,
}

impl Sort {
    fn label(self) -> &'static str {
        match self {
            Sort::Playtime => "played",
            Sort::Size => "size",
            Sort::Recent => "recent",
            Sort::Name => "name",
        }
    }
}

struct App {
    games: Vec<Game>,
    view: Vec<usize>, // indices into games, filtered+sorted
    state: TableState,
    sort: Sort,
    search: String,
    searching: bool,
    show_tools: bool,
    confirm: Option<usize>, // index into games pending uninstall
    status: String,
    quit: bool,
}

impl App {
    fn new(games: Vec<Game>) -> Self {
        let mut app = App {
            games,
            view: vec![],
            state: TableState::default(),
            sort: Sort::Playtime,
            search: String::new(),
            searching: false,
            show_tools: false,
            confirm: None,
            status: "↑/↓ move · p/s/r/n sort · / search · u uninstall · g refresh · q quit".into(),
            quit: false,
        };
        app.recompute();
        app.state.select(if app.view.is_empty() { None } else { Some(0) });
        app
    }

    fn recompute(&mut self) {
        let q = self.search.to_lowercase();
        let mut v: Vec<usize> = self
            .games
            .iter()
            .enumerate()
            .filter(|(_, g)| self.show_tools || !g.tool)
            .filter(|(_, g)| q.is_empty() || g.name.to_lowercase().contains(&q))
            .map(|(i, _)| i)
            .collect();
        let g = &self.games;
        v.sort_by(|&a, &b| {
            let (ga, gb) = (&g[a], &g[b]);
            match self.sort {
                Sort::Name => ga.name.to_lowercase().cmp(&gb.name.to_lowercase()),
                Sort::Size => gb.size.cmp(&ga.size),
                Sort::Recent => gb.last_played.cmp(&ga.last_played),
                Sort::Playtime => gb
                    .has_playtime
                    .cmp(&ga.has_playtime)
                    .then(gb.playtime.cmp(&ga.playtime))
                    .then(gb.size.cmp(&ga.size)),
            }
        });
        self.view = v;
        let n = self.view.len();
        match self.state.selected() {
            Some(_) if n == 0 => self.state.select(None),
            Some(s) if s >= n => self.state.select(Some(n - 1)),
            None if n > 0 => self.state.select(Some(0)),
            _ => {}
        }
    }

    fn selected_game(&self) -> Option<usize> {
        self.state.selected().and_then(|s| self.view.get(s).copied())
    }

    fn move_sel(&mut self, delta: i64) {
        let n = self.view.len();
        if n == 0 {
            return;
        }
        let cur = self.state.selected().unwrap_or(0) as i64;
        let next = (cur + delta).clamp(0, n as i64 - 1);
        self.state.select(Some(next as usize));
    }

    fn refresh(&mut self) {
        match load_games() {
            Ok(g) => {
                self.games = g;
                self.recompute();
                self.status = format!("refreshed — {} games", self.games.len());
            }
            Err(e) => self.status = format!("refresh failed: {e}"),
        }
    }

    fn do_uninstall(&mut self, gi: usize) {
        let uid = self.games[gi].uid.clone();
        let name = self.games[gi].name.clone();
        match run_uninstall(&uid) {
            Ok(msg) => {
                self.status = format!("{name}: {msg}");
                self.refresh();
            }
            Err(e) => self.status = format!("{name}: {e}"),
        }
    }
}

// ---------------------------------------------------------------------------
// UI
// ---------------------------------------------------------------------------

fn ui(f: &mut Frame, app: &mut App) {
    let area = f.area();
    let chunks = Layout::vertical([
        Constraint::Length(1), // title
        Constraint::Length(1), // stats
        Constraint::Min(0),    // table
        Constraint::Length(1), // status / search
    ])
    .split(area);

    // title
    let title = Line::from(vec![
        Span::styled("game", Style::default().fg(Color::Rgb(0, 240, 255)).bold()),
        Span::styled("stat", Style::default().fg(Color::Rgb(255, 59, 212)).bold()),
        Span::styled("  tui", Style::default().fg(Color::Rgb(109, 134, 168))),
    ]);
    f.render_widget(Paragraph::new(title), chunks[0]);

    // stats
    let total_size: u64 = app.view.iter().map(|&i| app.games[i].size).sum();
    let total_play: u64 = app
        .view
        .iter()
        .map(|&i| &app.games[i])
        .filter(|g| g.has_playtime)
        .map(|g| g.playtime)
        .sum();
    let sources: std::collections::BTreeSet<&str> =
        app.games.iter().map(|g| g.source.as_str()).collect();
    let stats = Line::from(vec![
        Span::styled(format!(" {} games ", app.view.len()), Style::default().fg(Color::White)),
        Span::styled("· ", Style::default().fg(Color::DarkGray)),
        Span::styled(format!("{} ", fmt_size(total_size)), Style::default().fg(Color::White)),
        Span::styled("on disk · ", Style::default().fg(Color::DarkGray)),
        Span::styled(format!("{} ", fmt_time(total_play, true)), Style::default().fg(Color::White)),
        Span::styled("played · ", Style::default().fg(Color::DarkGray)),
        Span::styled(
            sources.into_iter().collect::<Vec<_>>().join(" "),
            Style::default().fg(Color::Rgb(109, 134, 168)),
        ),
        Span::styled(
            format!("   sort: {}", app.sort.label()),
            Style::default().fg(Color::Rgb(0, 240, 255)),
        ),
    ]);
    f.render_widget(Paragraph::new(stats), chunks[1]);

    // table
    let max_size = app.view.iter().map(|&i| app.games[i].size).max().unwrap_or(1).max(1);
    let bar_w = 18usize;
    let rows: Vec<Row> = app
        .view
        .iter()
        .enumerate()
        .map(|(rank, &gi)| {
            let g = &app.games[gi];
            let col = source_color(&g.source);
            // name cell: [SOURCE] name  ☁
            let mut name_spans = vec![
                Span::styled(format!("{:<9}", g.source), Style::default().fg(col).bold()),
                Span::raw(" "),
                Span::styled(g.name.clone(), Style::default().fg(Color::Rgb(234, 246, 255))),
            ];
            if g.cloud {
                name_spans.push(Span::styled(" ☁", Style::default().fg(Color::Rgb(102, 192, 244))));
            }
            if g.tool {
                name_spans.push(Span::styled(" [tool]", Style::default().fg(Color::DarkGray)));
            }
            // disk bar
            let filled = ((g.size as f64 / max_size as f64) * bar_w as f64).round() as usize;
            let filled = filled.min(bar_w);
            let bar = Line::from(vec![
                Span::styled("█".repeat(filled), Style::default().fg(col)),
                Span::styled("░".repeat(bar_w - filled), Style::default().fg(Color::Rgb(28, 43, 64))),
            ]);
            Row::new(vec![
                Cell::from(format!("{:>2}", rank + 1)).style(Style::default().fg(Color::DarkGray)),
                Cell::from(Line::from(name_spans)),
                Cell::from(fmt_time(g.playtime, g.has_playtime)).style(Style::default().fg(Color::White)),
                Cell::from(fmt_size(g.size)).style(Style::default().fg(Color::White)),
                Cell::from(bar),
                Cell::from(fmt_date(g.last_played)).style(Style::default().fg(Color::Rgb(109, 134, 168))),
            ])
        })
        .collect();

    let widths = [
        Constraint::Length(3),
        Constraint::Min(24),
        Constraint::Length(8),
        Constraint::Length(9),
        Constraint::Length(bar_w as u16),
        Constraint::Length(10),
    ];
    let header = Row::new(vec!["#", "Game", "Played", "Size", "Disk", "Last"])
        .style(Style::default().fg(Color::Rgb(109, 134, 168)).add_modifier(Modifier::BOLD));
    let table = Table::new(rows, widths)
        .header(header)
        .row_highlight_style(
            Style::default()
                .bg(Color::Rgb(20, 34, 52))
                .add_modifier(Modifier::BOLD),
        )
        .highlight_symbol("▶ ");
    f.render_stateful_widget(table, chunks[2], &mut app.state);

    // status / search
    if app.searching {
        let s = Line::from(vec![
            Span::styled("/", Style::default().fg(Color::Rgb(0, 240, 255))),
            Span::raw(app.search.clone()),
            Span::styled("▏", Style::default().fg(Color::Rgb(0, 240, 255))),
        ]);
        f.render_widget(Paragraph::new(s), chunks[3]);
    } else {
        f.render_widget(
            Paragraph::new(Span::styled(
                app.status.clone(),
                Style::default().fg(Color::Rgb(109, 134, 168)),
            )),
            chunks[3],
        );
    }

    // confirm modal
    if let Some(gi) = app.confirm {
        let g = &app.games[gi];
        let popup = centered_rect(60, 40, area);
        f.render_widget(Clear, popup);
        let col = source_color(&g.source);
        let cloud_line = if g.source == "Steam" {
            if g.cloud {
                Line::from(vec![
                    Span::styled("Cloud save: ", Style::default().fg(Color::DarkGray)),
                    Span::styled("yes — safe", Style::default().fg(Color::Rgb(102, 192, 244))),
                ])
            } else {
                Line::from(vec![
                    Span::styled("Cloud save: ", Style::default().fg(Color::DarkGray)),
                    Span::styled("none — local saves may be lost", Style::default().fg(Color::Rgb(255, 207, 107))),
                ])
            }
        } else {
            Line::from(Span::styled(
                format!("Removed via {}.", g.source),
                Style::default().fg(Color::DarkGray),
            ))
        };
        let body = vec![
            Line::from(vec![
                Span::styled(format!("{} ", g.source), Style::default().fg(col).bold()),
                Span::styled(g.name.clone(), Style::default().fg(Color::White).bold()),
            ]),
            Line::from(vec![
                Span::styled("Size on disk: ", Style::default().fg(Color::DarkGray)),
                Span::styled(fmt_size(g.size), Style::default().fg(Color::White)),
            ]),
            cloud_line,
            Line::raw(""),
            Line::from(vec![
                Span::styled("Uninstall? ", Style::default().fg(Color::White)),
                Span::styled("[y]", Style::default().fg(Color::Rgb(255, 59, 212)).bold()),
                Span::styled("es  ", Style::default().fg(Color::DarkGray)),
                Span::styled("[n]", Style::default().fg(Color::Rgb(0, 240, 255)).bold()),
                Span::styled("o", Style::default().fg(Color::DarkGray)),
            ]),
        ];
        let block = Block::bordered()
            .border_type(BorderType::Rounded)
            .border_style(Style::default().fg(Color::Rgb(255, 59, 212)))
            .title(" Uninstall ");
        f.render_widget(
            Paragraph::new(body).block(block).wrap(Wrap { trim: true }),
            popup,
        );
    }
}

fn centered_rect(pct_x: u16, pct_y: u16, area: Rect) -> Rect {
    let v = Layout::vertical([
        Constraint::Percentage((100 - pct_y) / 2),
        Constraint::Percentage(pct_y),
        Constraint::Percentage((100 - pct_y) / 2),
    ])
    .split(area);
    Layout::horizontal([
        Constraint::Percentage((100 - pct_x) / 2),
        Constraint::Percentage(pct_x),
        Constraint::Percentage((100 - pct_x) / 2),
    ])
    .split(v[1])[1]
}

// ---------------------------------------------------------------------------
// Event handling
// ---------------------------------------------------------------------------

fn on_key(app: &mut App, code: KeyCode) {
    // modal takes priority
    if let Some(gi) = app.confirm {
        match code {
            KeyCode::Char('y') | KeyCode::Char('Y') => {
                app.confirm = None;
                app.do_uninstall(gi);
            }
            KeyCode::Char('n') | KeyCode::Char('N') | KeyCode::Esc => app.confirm = None,
            _ => {}
        }
        return;
    }
    if app.searching {
        match code {
            KeyCode::Esc => {
                app.searching = false;
                app.search.clear();
                app.recompute();
            }
            KeyCode::Enter => app.searching = false,
            KeyCode::Backspace => {
                app.search.pop();
                app.recompute();
            }
            KeyCode::Char(c) => {
                app.search.push(c);
                app.recompute();
            }
            _ => {}
        }
        return;
    }
    match code {
        KeyCode::Char('q') | KeyCode::Esc => app.quit = true,
        KeyCode::Down | KeyCode::Char('j') => app.move_sel(1),
        KeyCode::Up | KeyCode::Char('k') => app.move_sel(-1),
        KeyCode::Char('p') => {
            app.sort = Sort::Playtime;
            app.recompute();
        }
        KeyCode::Char('s') => {
            app.sort = Sort::Size;
            app.recompute();
        }
        KeyCode::Char('r') => {
            app.sort = Sort::Recent;
            app.recompute();
        }
        KeyCode::Char('n') => {
            app.sort = Sort::Name;
            app.recompute();
        }
        KeyCode::Char('t') => {
            app.show_tools = !app.show_tools;
            app.recompute();
        }
        KeyCode::Char('g') => app.refresh(),
        KeyCode::Char('/') => {
            app.searching = true;
            app.search.clear();
        }
        KeyCode::Char('u') | KeyCode::Char('d') => {
            if let Some(gi) = app.selected_game() {
                app.confirm = Some(gi);
            }
        }
        _ => {}
    }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------

fn run() -> Result<(), String> {
    let games = load_games()?;
    let mut app = App::new(games);
    let mut terminal = ratatui::init();
    let res = event_loop(&mut terminal, &mut app);
    ratatui::restore();
    res
}

fn event_loop<B: ratatui::backend::Backend>(
    terminal: &mut Terminal<B>,
    app: &mut App,
) -> Result<(), String> {
    while !app.quit {
        terminal
            .draw(|f| ui(f, app))
            .map_err(|e| format!("draw error: {e}"))?;
        if event::poll(Duration::from_millis(200)).map_err(|e| e.to_string())? {
            if let Event::Key(k) = event::read().map_err(|e| e.to_string())? {
                if k.kind == KeyEventKind::Press {
                    on_key(app, k.code);
                }
            }
        }
    }
    Ok(())
}

/// Render one frame to an offscreen buffer and print it — no TTY needed.
/// `keys` is an optional script of characters fed through on_key first, so the
/// non-interactive test can exercise sorting, search, and the modal.
fn selftest(keys: &str) -> Result<(), String> {
    let games = load_games()?;
    let mut app = App::new(games);
    for c in keys.chars() {
        on_key(&mut app, KeyCode::Char(c));
    }
    let backend = TestBackend::new(110, 30);
    let mut terminal = Terminal::new(backend).map_err(|e| e.to_string())?;
    terminal.draw(|f| ui(f, &mut app)).map_err(|e| e.to_string())?;
    let buf = terminal.backend().buffer().clone();
    let mut out = String::new();
    for y in 0..buf.area.height {
        for x in 0..buf.area.width {
            out.push_str(buf[(x, y)].symbol());
        }
        out.push('\n');
    }
    print!("{out}");
    let _ = std::io::stdout().flush();
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let result = if args.iter().any(|a| a == "--selftest") {
        let keys = args
            .iter()
            .find_map(|a| a.strip_prefix("--keys="))
            .unwrap_or("");
        selftest(keys)
    } else if args.iter().any(|a| a == "--help" || a == "-h") {
        println!(
            "gamestat-tui — terminal UI for gamestat\n\n\
             Keys: ↑/↓ move · p/s/r/n sort · / search · t tools · u uninstall · g refresh · q quit\n\
             Env:  GAMESTAT_BIN  path to the gamestat executable (default: gamestat on PATH)"
        );
        Ok(())
    } else {
        run()
    };
    if let Err(e) = result {
        eprintln!("gamestat-tui: {e}");
        std::process::exit(1);
    }
}
