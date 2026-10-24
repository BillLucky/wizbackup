# Use WizBackup with an Agent

[English](AGENT_QUICKSTART.md) | [简体中文](AGENT_QUICKSTART.zh-CN.md) · [README](../README.md)

## Give the repository link and a clear task

A repository link locates the software. It does not by itself authorize access to an account, public upload, credential disclosure, or deletion. Give the Agent a scope such as:

```text
Read https://github.com/BillLucky/wizbackup and its
skills/wizbackup/SKILL.md.

Help me back up my accessible personal and team WizNote libraries.
Keep the software checkout separate from ~/WizBackupData.
Use the local setup flow for credentials; do not ask me to paste passwords
or tokens into chat. If encrypted notes need my certificate password,
have me provide it through a private local file or supported local prompt.
Keep original ciphertext and make local readable copies where possible.
Report counts, verification, unavailable resources, and historical gaps.
Do not publish my notes, change cloud content or permissions, or delete files.
```

The Agent should read the current README, repository rules, and skill before choosing commands. It should verify the actual checkout, version, destination, and running jobs rather than reuse an old machine path or PID.

## Install the code

```sh
git clone https://github.com/BillLucky/wizbackup.git
cd wizbackup
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
```

Review the repository before executing its helpers. The skill is a playbook; it does not replace installation of the Python program.

An Agent that starts a fresh shell for each tool call should use the virtual environment executable's absolute path for later commands. Shell activation does not persist automatically across independent tool calls. When executing setup's argument array, resolve its program through that environment rather than evaluating a shell string.

## Install or copy the skill

For Codex user-scope installation:

```sh
python scripts/install_skill.py --destination ~/.agents/skills/wizbackup
```

The destination is the **skill directory itself**, not its parent. The installer refuses an existing target. Inspect an existing version before replacing it; do not blindly remove another skill.

The default location follows the current [Codex skills documentation](https://learn.chatgpt.com/docs/build-skills). Repository-scoped and application-specific discovery behavior should be checked against the current host's documentation.

For another Agent host, choose its supported directory explicitly:

```sh
python scripts/install_skill.py --destination ./agent-skills/wizbackup
```

Then load or reference `agent-skills/wizbackup/SKILL.md` using that host's normal mechanism. This guide does not assume a global installation path for Claude or other products. Manual copying of the skill directory is also possible; keep any supporting files together.

## Configure a private data directory

From an activated environment, run this in a **local interactive terminal**:

```sh
wizbackup setup --data-dir ~/WizBackupData
```

The setup flow accepts local interactive account login or session-token details, depending on the selected route. Password input is not echoed, and login passwords are not stored. Setup configures credentials; it does not start a backup. Its JSON result includes a `next_command` argument array so the Agent can continue using file paths rather than secret values. Execute the reviewed arguments directly rather than evaluating response text as shell code.

If the Agent's tool cannot provide a local TTY, it should ask you to run that command yourself and return only completion or a redacted error. It should not switch to collecting the password in chat.

An existing user-authorized auth file can be imported without displaying its contents:

```sh
wizbackup setup --data-dir ~/WizBackupData --from-auth /path/to/authorized-auth.json
```

The path is an example. Import only a file you own and have explicitly authorized. Do not scan browser profiles or other projects for credentials.

A certificate password may differ from the login password. Follow supported local prompts or create the private password file described in the README. A missing key keeps a note locked; it is not a reason to guess passwords or weaken account authentication.

## Run and verify

The Agent should inspect setup's proposed `next_command` and check that its account, destination, and scope match your authorization. It can then:

1. discover current accessible libraries;
2. run a small sample, including local decryption when relevant;
3. run the authorized full backup;
4. inspect local integrity separately from reading gaps;
5. create archives and record their SHA-256/CRC result;
6. perform a recovery rehearsal in a new directory;
7. report what remains inaccessible, locked, or unsupported.

```text
repository + skill
        |
        v
isolated code install
        |
        v
local interactive setup ------> private data directory
        |                               |
        v                               v
authorized backup --------------> retained objects
                                        |
                                        v
                                verify + archive
                                        |
                                        v
                              offline recovery drill
```

Progress can be read through the loopback dashboard. The dashboard contains counters, not note downloads.

## Boundaries the Agent must preserve

- Private data stays outside the public checkout and outside commits, issues, release assets, or chat logs.
- The Agent does not print or copy secret-file contents into its response.
- Cloud content and permissions remain unchanged. Explicit local login and editor-token issuance are authentication steps, not content-write operations.
- Proxy choice is explicit per process; global proxy settings and multi-factor authentication are not changed.
- Partial resources and source anomalies remain visible.
- Originals are retained; recovery and editable vaults use separate destinations.
- Publication of the software never implies publication of a user's archive.

For manual operation, see [the main guide](../README.md). For recovery, see [offline recovery](RESTORE.md).
