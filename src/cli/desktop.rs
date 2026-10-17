//! Desktop history discovery is on demand, outside the server/render hot paths.
//! The helper owns platform storage/transport; this client owns only selection.
use std::io::{self, IsTerminal};

use clap::{Arg, Command};
use crossterm::{
    event::{self, Event, KeyCode, KeyEventKind, KeyModifiers, MouseEventKind},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Layout},
    style::{Modifier, Style},
    widgets::{Block, Borders, List, ListItem, ListState, Paragraph},
    Terminal,
};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
struct Chat {
    provider: String,
    id: String,
    title: String,
    cwd: String,
}

#[derive(Debug, Deserialize)]
struct Catalog {
    chats: Vec<Chat>,
    warnings: Vec<String>,
}

pub(super) fn command() -> Command {
    let provider = || Arg::new("provider").value_parser(["work", "claude", "antigravity"]);
    Command::new("desktop")
        .about("Continue an existing local desktop chat without copying it (macOS)")
        .subcommand(
            Command::new("pick")
                .about("Choose a desktop chat interactively")
                .arg(provider()),
        )
        .subcommand(
            Command::new("list")
                .about("List desktop chats as JSON")
                .arg(provider()),
        )
        .subcommand(
            Command::new("open").about("Open the desktop chat picker in a new Herdr workspace"),
        )
        .subcommand(
            Command::new("resume")
                .about("Continue the exact existing chat ID")
                .arg(provider().required(true))
                .arg(Arg::new("id").required(true)),
        )
}

pub(super) fn run(args: &[String]) -> io::Result<i32> {
    let matches = match command()
        .try_get_matches_from(std::iter::once("desktop".to_string()).chain(args.iter().cloned()))
    {
        Ok(matches) => matches,
        Err(error) => {
            error.print()?;
            return Ok(error.exit_code());
        }
    };
    let (action, provider, id) = match matches.subcommand() {
        Some((action, sub)) => (
            action,
            sub.try_get_one::<String>("provider")
                .ok()
                .flatten()
                .cloned(),
            sub.try_get_one::<String>("id").ok().flatten().cloned(),
        ),
        None => ("pick", None, None),
    };
    if action == "open" {
        return open_workspace();
    }
    if action == "resume" {
        let mut helper = crate::platform::desktop_chat_command()?;
        helper.arg("resume").args(provider).args(id);
        return Ok(helper.status()?.code().unwrap_or(1));
    }
    let output = crate::platform::desktop_chat_command()?
        .arg("list")
        .args(provider.as_ref())
        .output()?;
    if !output.status.success() {
        eprint!("{}", String::from_utf8_lossy(&output.stderr));
        return Ok(1);
    }
    if action == "list" {
        print!("{}", String::from_utf8_lossy(&output.stdout));
        return Ok(0);
    }
    let catalog: Catalog = serde_json::from_slice(&output.stdout).map_err(io::Error::other)?;
    if !io::stdin().is_terminal() || !io::stdout().is_terminal() {
        return Err(io::Error::other(
            "Desktop picker needs a terminal; use herdr desktop list or herdr desktop open",
        ));
    }
    if let Some(index) = pick(&catalog)? {
        let chat = &catalog.chats[index];
        return Ok(crate::platform::desktop_chat_command()?
            .args(["resume", &chat.provider, &chat.id])
            .status()?
            .code()
            .unwrap_or(1));
    }
    Ok(0)
}

fn open_workspace() -> io::Result<i32> {
    use crate::api::schema::{Method, PaneSendInputParams, Request, WorkspaceCreateParams};
    let command = crate::platform::desktop_picker_shell_command()?;
    let client = super::target::api_client()?;
    let response = client
        .request_value(&Request {
            id: "desktop:create".into(),
            method: Method::WorkspaceCreate(WorkspaceCreateParams {
                source_workspace_id: None,
                cwd: None,
                focus: true,
                label: Some("Desktop chats".into()),
                env: Default::default(),
            }),
        })
        .map_err(io::Error::other)?;
    let pane = response
        .pointer("/result/root_pane/pane_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| io::Error::other(format!("Could not open desktop workspace: {response}")))?;
    let response = client
        .request_value(&Request {
            id: "desktop:pick".into(),
            method: Method::PaneSendInput(PaneSendInputParams {
                pane_id: pane.into(),
                text: command,
                keys: vec!["Enter".into()],
            }),
        })
        .map_err(io::Error::other)?;
    if response.get("error").is_some() {
        return Err(io::Error::other(response.to_string()));
    }
    Ok(0)
}

fn filtered(catalog: &Catalog, query: &str) -> Vec<usize> {
    let query = query.to_lowercase();
    catalog
        .chats
        .iter()
        .enumerate()
        .filter(|(_, c)| {
            format!("{} {} {} {}", c.provider, c.title, c.cwd, c.id)
                .to_lowercase()
                .contains(&query)
        })
        .map(|(i, _)| i)
        .collect()
}

struct Screen;
impl Drop for Screen {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let _ = execute!(
            io::stdout(),
            event::DisableMouseCapture,
            LeaveAlternateScreen
        );
    }
}

fn pick(catalog: &Catalog) -> io::Result<Option<usize>> {
    enable_raw_mode()?;
    let _screen = Screen;
    execute!(
        io::stdout(),
        EnterAlternateScreen,
        event::EnableMouseCapture
    )?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;
    let mut query = String::new();
    let mut selection = ListState::default().with_selected(Some(0));
    loop {
        let visible = filtered(catalog, &query);
        let selected = selection
            .selected()
            .unwrap_or(0)
            .min(visible.len().saturating_sub(1));
        selection.select(Some(selected));
        let mut rows = ratatui::layout::Rect::default();
        terminal.draw(|frame| {
            let areas = Layout::vertical([
                Constraint::Length(3),
                Constraint::Min(1),
                Constraint::Length(4),
            ])
            .split(frame.area());
            frame.render_widget(
                Paragraph::new(format!("Search: {query}")).block(
                    Block::default()
                        .borders(Borders::ALL)
                        .title(" Desktop chats · continue without a copy "),
                ),
                areas[0],
            );
            rows = areas[1];
            let items = visible
                .iter()
                .map(|i| {
                    let c = &catalog.chats[*i];
                    let label = match c.provider.as_str() {
                        "work" => "ChatGPT Work",
                        "claude" => "Claude Code",
                        _ => "Antigravity App",
                    };
                    ListItem::new(format!("{label:<16} {}", c.title))
                })
                .collect::<Vec<_>>();
            frame.render_stateful_widget(
                List::new(items)
                    .highlight_style(Style::default().add_modifier(Modifier::REVERSED))
                    .highlight_symbol("› "),
                rows,
                &mut selection,
            );
            let identity = visible
                .get(selected)
                .map(|i| catalog.chats[*i].id.as_str())
                .unwrap_or("No matching chats");
            let notice = format!(
                "{} chats · Enter/click continue · ↑↓ move · type to search · Esc close\n{}\n{}",
                visible.len(),
                identity,
                catalog.warnings.join(" | ")
            );
            frame.render_widget(
                Paragraph::new(notice).block(Block::default().borders(Borders::TOP)),
                areas[2],
            );
        })?;
        match event::read()? {
            Event::Key(key) if key.kind != KeyEventKind::Release => match key.code {
                KeyCode::Esc => return Ok(None),
                KeyCode::Char('c') if key.modifiers.contains(KeyModifiers::CONTROL) => {
                    return Ok(None)
                }
                KeyCode::Enter => {
                    if let Some(i) = visible.get(selected) {
                        return Ok(Some(*i));
                    }
                }
                KeyCode::Up => selection.select(Some(selected.saturating_sub(1))),
                KeyCode::Down => {
                    selection.select(Some((selected + 1).min(visible.len().saturating_sub(1))))
                }
                KeyCode::Backspace => {
                    query.pop();
                    selection.select(Some(0));
                }
                KeyCode::Char(c)
                    if !key
                        .modifiers
                        .intersects(KeyModifiers::CONTROL | KeyModifiers::ALT) =>
                {
                    query.push(c);
                    selection.select(Some(0));
                }
                _ => {}
            },
            Event::Mouse(mouse) => match mouse.kind {
                MouseEventKind::ScrollUp => selection.select(Some(selected.saturating_sub(1))),
                MouseEventKind::ScrollDown => {
                    selection.select(Some((selected + 1).min(visible.len().saturating_sub(1))))
                }
                MouseEventKind::Down(event::MouseButton::Left)
                    if rows.contains((mouse.column, mouse.row).into()) =>
                {
                    let i = selection.offset() + usize::from(mouse.row - rows.y);
                    if let Some(i) = visible.get(i) {
                        return Ok(Some(*i));
                    }
                }
                _ => {}
            },
            _ => {}
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn desktop_search_preserves_exact_identity() {
        let c = Catalog {
            chats: vec![Chat {
                provider: "claude".into(),
                id: "abc".into(),
                title: "Привет".into(),
                cwd: "/tmp".into(),
            }],
            warnings: vec![],
        };
        assert_eq!(filtered(&c, "ПРИВ"), vec![0]);
        assert_eq!(filtered(&c, "abc"), vec![0]);
        assert!(filtered(&c, "work").is_empty());
    }
}
