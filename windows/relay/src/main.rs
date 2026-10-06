//! Stands in for Codex's voice helper on a Windows work computer.
//!
//! The plugin places this program only in its private copy of a Codex package,
//! as `codex-resources/voice/bin/codex-voice-host.exe`; installed Codex files
//! are never changed. Codex starts it exactly as it starts the real helper. It
//! runs one `ssh -T` to the microphone computer, where the real helper with the
//! same build runs, and the helper's binary stdin and stdout pass through
//! unchanged because ssh.exe inherits them.
//!
//! Everything it runs comes from `codex-voice-host.relay` beside it, written by
//! the plugin: the absolute path of ssh.exe, a few allowlisted environment
//! variables, and each argument. Nothing is interpreted by a shell here.

use std::env;
use std::fs::File;
use std::io::Read;
use std::path::PathBuf;
use std::process::{self, Command, Stdio};

const MAGIC: &str = "herdr-codex-voice-relay 1";
const MAX_CONFIG_BYTES: u64 = 64 * 1024;
const MAX_ARGS: usize = 64;
const MAX_ARG_BYTES: usize = 8 * 1024;
/// What ssh.exe needs to find the user's profile, configuration, keys and agent.
const ENVIRONMENT: &[&str] = &[
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "USERNAME", "USERDOMAIN", "SYSTEMROOT",
    "WINDIR", "SYSTEMDRIVE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "TEMP", "TMP",
];

#[derive(Debug, PartialEq)]
struct Config {
    build_commit: String,
    ssh: PathBuf,
    env: Vec<(String, String)>,
    args: Vec<String>,
}

fn valid_commit(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 64
        && value.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'.' || b == b'-' || b == b'_')
}

fn parse(text: &str) -> Result<Config, String> {
    let mut lines = text.split('\n');
    if lines.next() != Some(MAGIC) {
        return Err("not a relay configuration".into());
    }
    let mut build_commit = None;
    let mut ssh = None;
    let mut env = Vec::new();
    let mut args = Vec::new();
    for line in lines {
        if line.is_empty() {
            continue;
        }
        if line.contains('\r') || line.contains('\0') {
            return Err("configuration contains a control character".into());
        }
        let (key, value) = line.split_once('=').ok_or("malformed configuration line")?;
        match key {
            "build_commit" if build_commit.is_none() && valid_commit(value) => {
                build_commit = Some(value.to_string())
            }
            "ssh" if ssh.is_none() => ssh = Some(PathBuf::from(value)),
            "env" => {
                let (name, value) = value.split_once('=').ok_or("malformed environment line")?;
                if !ENVIRONMENT.contains(&name) || env.iter().any(|(seen, _): &(String, String)| seen == name) {
                    return Err(format!("environment variable {name} is not allowed"));
                }
                env.push((name.to_string(), value.to_string()));
            }
            "arg" if args.len() < MAX_ARGS && value.len() <= MAX_ARG_BYTES => args.push(value.to_string()),
            _ => return Err(format!("unexpected {key} line")),
        }
    }
    let build_commit = build_commit.ok_or("missing build_commit")?;
    let ssh = ssh.ok_or("missing ssh")?;
    let is_ssh = ssh
        .file_name()
        .and_then(|name| name.to_str())
        .map_or(false, |name| name.eq_ignore_ascii_case("ssh.exe"));
    if !ssh.is_absolute() || !is_ssh {
        return Err("ssh must be an absolute path to ssh.exe".into());
    }
    if args.is_empty() {
        return Err("missing ssh arguments".into());
    }
    Ok(Config { build_commit, ssh, env, args })
}

fn load() -> Result<Config, String> {
    let exe = env::current_exe().map_err(|error| error.to_string())?;
    let path = exe.with_extension("relay");
    let file = File::open(&path).map_err(|error| format!("{}: {error}", path.display()))?;
    let mut text = String::new();
    file.take(MAX_CONFIG_BYTES + 1)
        .read_to_string(&mut text)
        .map_err(|error| format!("{}: {error}", path.display()))?;
    if text.len() as u64 > MAX_CONFIG_BYTES {
        return Err(format!("{} is too large", path.display()));
    }
    let config = parse(&text)?;
    if !config.ssh.is_file() {
        return Err(format!("{} is missing", config.ssh.display()));
    }
    Ok(config)
}

/// Puts this process, and so every child it starts, in a job that is killed
/// when its last handle closes. The handle is deliberately kept open until this
/// process exits, so ssh.exe cannot outlive the relay when Codex stops it.
#[cfg(windows)]
fn contain_children() -> Result<(), String> {
    use std::ffi::c_void;
    use std::io::Error;
    use std::mem::{size_of, zeroed};
    use std::ptr::{null, null_mut};

    // JOBOBJECT_BASIC_LIMIT_INFORMATION and JOBOBJECT_EXTENDED_LIMIT_INFORMATION.
    #[allow(dead_code)]
    #[repr(C)]
    struct BasicLimitInformation {
        per_process_user_time_limit: i64,
        per_job_user_time_limit: i64,
        limit_flags: u32,
        minimum_working_set_size: usize,
        maximum_working_set_size: usize,
        active_process_limit: u32,
        affinity: usize,
        priority_class: u32,
        scheduling_class: u32,
    }
    #[allow(dead_code)]
    #[repr(C)]
    struct ExtendedLimitInformation {
        basic: BasicLimitInformation,
        io_counters: [u64; 6],
        process_memory_limit: usize,
        job_memory_limit: usize,
        peak_process_memory_used: usize,
        peak_job_memory_used: usize,
    }
    const JOB_OBJECT_EXTENDED_LIMIT_INFORMATION: i32 = 9;
    const JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: u32 = 0x2000;
    #[link(name = "kernel32")]
    extern "system" {
        fn CreateJobObjectW(attributes: *mut c_void, name: *const u16) -> *mut c_void;
        fn SetInformationJobObject(job: *mut c_void, class: i32, information: *const c_void, length: u32) -> i32;
        fn AssignProcessToJobObject(job: *mut c_void, process: *mut c_void) -> i32;
        fn GetCurrentProcess() -> *mut c_void;
        fn CloseHandle(handle: *mut c_void) -> i32;
    }
    unsafe {
        let job = CreateJobObjectW(null_mut(), null());
        if job.is_null() {
            return Err(format!("could not create a job object: {}", Error::last_os_error()));
        }
        let mut limits: ExtendedLimitInformation = zeroed();
        limits.basic.limit_flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let length = size_of::<ExtendedLimitInformation>() as u32;
        let information = &limits as *const ExtendedLimitInformation as *const c_void;
        if SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, information, length) == 0 {
            let error = Error::last_os_error();
            CloseHandle(job);
            return Err(format!("could not configure the job object: {error}"));
        }
        if AssignProcessToJobObject(job, GetCurrentProcess()) == 0 {
            let error = Error::last_os_error();
            CloseHandle(job);
            return Err(format!("could not join the job object: {error}"));
        }
    }
    Ok(())
}

#[cfg(not(windows))]
fn contain_children() -> Result<(), String> {
    Ok(())
}

fn main() {
    let mut arguments = env::args_os().skip(1);
    let first = arguments.next();
    if arguments.next().is_some() {
        process::exit(2);
    }
    let config = match load() {
        Ok(config) => config,
        Err(error) => {
            eprintln!("Codex Voice relay: {error}; rerun codex-voice setup");
            process::exit(127);
        }
    };
    match first {
        // Answers like the real helper, for the build it relays to.
        Some(argument) if argument == "--build-commit" => {
            println!("{}", config.build_commit);
            return;
        }
        Some(_) => process::exit(2),
        None => {}
    }
    if let Err(error) = contain_children() {
        // Without containment ssh.exe could outlive the helper, so it is not started.
        eprintln!("Codex Voice relay: {error}");
        process::exit(126);
    }
    let status = Command::new(&config.ssh)
        .args(&config.args)
        .envs(config.env.iter().cloned())
        .stdin(Stdio::inherit())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .status();
    match status {
        Ok(status) => process::exit(status.code().unwrap_or(1)),
        Err(error) => {
            eprintln!("Codex Voice relay: {}: {error}", config.ssh.display());
            process::exit(127);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn ssh_path() -> &'static str {
        if cfg!(windows) {
            r"C:\Windows\System32\OpenSSH\ssh.exe"
        } else {
            "/usr/bin/ssh.exe"
        }
    }

    fn config(extra: &str) -> String {
        format!("{MAGIC}\nbuild_commit=abc123\nssh={}\narg=-T\narg=mic\narg=python3 helper run\n{extra}", ssh_path())
    }

    #[test]
    fn accepts_the_plugins_configuration() {
        let parsed = parse(&config("env=USERPROFILE=C:\\Users\\a b\n")).unwrap();
        assert_eq!(parsed.build_commit, "abc123");
        assert_eq!(parsed.args, ["-T", "mic", "python3 helper run"]);
        assert_eq!(parsed.env, [("USERPROFILE".to_string(), "C:\\Users\\a b".to_string())]);
    }

    #[test]
    fn rejects_anything_else() {
        for text in [
            config("").replacen(MAGIC, "other", 1),
            config("command=calc.exe\n"),
            config("env=PATH=C:\\elsewhere\n"),
            config("env=TEMP=a\nenv=TEMP=b\n"),
            config("arg=a\r\n"),
            config("build_commit=again\n"),
            config("").replace(ssh_path(), "ssh.exe"),
            config("").replace(ssh_path(), &ssh_path().replace("ssh.exe", "cmd.exe")),
            format!("{MAGIC}\nbuild_commit=abc\nssh={}\n", ssh_path()),
            config(&"arg=x\n".repeat(MAX_ARGS)),
        ] {
            assert!(parse(&text).is_err(), "{text}");
        }
    }

    #[test]
    fn build_commits_are_plain_identifiers() {
        assert!(valid_commit("c0ffee"));
        assert!(valid_commit("dev"));
        assert!(!valid_commit(""));
        assert!(!valid_commit("abc def"));
        assert!(!valid_commit(&"a".repeat(65)));
    }
}
