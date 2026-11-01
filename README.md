# The Axiom

The Axiom is a self-hosted project for preparing and serving a curated offline reference library over a local network.

It is intended for households, community groups, schools, makerspaces, and other organizations that want useful reference material available when Internet access is unavailable. A locally hosted library can be browsed from nearby phones, tablets, and computers using hardware and storage managed by the operator.

The Axiom is software for building and operating a library, not a preloaded collection of books, archives, or media. Operators choose the content, verify its source and redistribution rights, provide suitable hardware and storage, and decide how the library is made available on their local network.

## Intended workflow

1. **Curate content.** Select reference materials and record where they came from, their licensing terms, and other relevant metadata.
2. **Prepare a release.** Stage selected materials, verify their integrity, and organize them into a library that is ready to publish.
3. **Serve locally.** Make the prepared library available through a local web interface so users on the network can browse it without relying on the public Internet.

Preparation and serving can be performed on one machine or separated across multiple systems, depending on the installation. The project is designed to be adaptable to different hardware and operating environments.

## Content and safety

The project does not grant rights to third-party content. Before including or redistributing any resource, verify its license, attribution requirements, and any restrictions on use. Preserve source and version information so operators can identify what is in their library and where it came from.

Content quality matters, especially for emergency, health, water, sanitation, and other safety-related topics. Operators are responsible for choosing trustworthy sources and reviewing the materials they publish. The Axiom is a way to make selected resources available offline; it is not a substitute for professional advice or official emergency guidance.

## Project status

The Axiom is in early development. Its goals include reliable preparation and local serving of curated reference materials, with clear provenance and explicit operator control over what is published. It is not yet a production-ready appliance or a finished, universally supported installation package.

This repository is the standalone home for the project. The current migration preserves the application and proof-of-concept tooling while intentionally excluding Ansible, host inventory, nginx/systemd deployment files, and Projectwright integration. See `docs/architecture.md` and `docs/backlog.md` for boundaries and next work.

## Development

The supported development baseline is Ubuntu 24.04 LTS and Ubuntu 26.04 LTS with Python 3.10 or newer:

```shell
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest
```

The test suite is local and deterministic by default. Fixtures and generated reports are used to exercise the preserved POC behavior without requiring Ansible, nginx, systemd, Projectwright, or outbound network access.

## License

A project license has not yet been selected. Until a license is added, do not assume that the project may be redistributed or reused under open-source terms. Third-party library content may have separate rights and license terms.
