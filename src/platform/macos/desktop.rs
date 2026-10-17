use std::io;
use std::process::Command;

pub(crate) fn desktop_chat_command() -> io::Result<Command> {
    // Apple's older Python/SQLite can reject a live Antigravity WAL database.
    // Prefer an up-to-date local runtime; never install or change one here.
    let python = std::env::var_os("HERDR_DESKTOP_PYTHON")
        .filter(|path| !path.is_empty())
        .or_else(|| {
            ["/opt/homebrew/bin/python3", "/usr/local/bin/python3"]
                .into_iter()
                .find(|path| std::path::Path::new(path).is_file())
                .map(std::ffi::OsString::from)
        })
        .unwrap_or_else(|| "python3".into());
    let mut command = Command::new(python);
    command.args(["-c", include_str!("desktop/bridge.py")]);
    command.env("HERDR_DESKTOP_EXE", std::env::current_exe()?);
    Ok(command)
}

pub(crate) fn desktop_picker_shell_command() -> io::Result<String> {
    let executable = std::env::current_exe()?;
    let executable = executable
        .to_str()
        .ok_or_else(|| io::Error::other("Herdr executable path is not UTF-8"))?;
    // POSIX shells, including fish, accept single-quoted literal arguments.
    Ok(format!(
        "'{}' desktop pick",
        executable.replace('\'', "'\\''")
    ))
}
