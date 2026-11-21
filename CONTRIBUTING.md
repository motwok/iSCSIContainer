# Contributing to iSCSIContainer

Thanks for your interest in iSCSIContainer!

This project follows a [Code of Conduct](CODE_OF_CONDUCT.md); please be respectful.

> **Security issue?** Do **not** open a public issue; report it privately, see [SECURITY.md](SECURITY.md).

## Ways to contribute

- **Bug reports**: [open an issue](https://github.com/motwok/iSCSIContainer/issues)
- **Feature requests**: [open an issue](https://github.com/motwok/iSCSIContainer/issues)
- **Pull requests** are welcome, see below.

Ideas, reproductions and well-described problems are genuinely valuable too.

## Pull requests

iSCSIContainer runs privileged, loads kernel modules and manages the kernel's iSCSI target, which holds
other people's data. Every change is reviewed carefully before it is merged. To make that easy:

- For anything larger than a small fix, open an issue first so the approach can be agreed on.
- Keep a pull request focused on one change. Work on a branch of your fork and target `master`.
- Use a Conventional Commit style title, for example `feat: ...`, `fix: ...`, `docs: ...`.
- Write code, comments and documentation in English. New dependencies need to be agreed in an issue
  first.
- Do not put raw non-ASCII characters into source code; write them as escape sequences (`\uXXXX`).
- Diagrams in Markdown are Mermaid, not ASCII art.
- The build, including the tests, must be green.

By submitting a pull request you agree that your contribution is licensed under the
**GNU AGPL v3** (see [LICENSE](LICENSE)).

## Writing a good bug report

Please include:

- **What happened** and **what you expected**.
- The **iSCSIContainer version**: the image tag or digest.
- The **host**: distribution, kernel (`uname -r`) and **Docker version**.
- The **initiator**: operating system and version, and whether MC/S or MPIO is used.
- The **backstore type** (file or block device).
- The relevant lines of the **container log** (`docker logs <container>`) and, if useful, the
  YAML configuration.
- **Steps to reproduce**, if you can; even rough ones help.

Remove IQNs, CHAP secrets, host names and addresses you do not want to make public before posting.

## Feature requests

Describe the problem you're trying to solve, not just a proposed solution; it helps find the best fit.
Note that not every request will be implemented; iSCSIContainer intentionally stays small and focused.

## Forking

iSCSIContainer is licensed under the **GNU AGPL v3** (see [LICENSE](LICENSE)). You are of course free to
fork and modify it under those terms.

## Support Me

If iSCSIContainer helps you, you can support it: [Buy Me a Coffee](https://ko-fi.com/motwok)
