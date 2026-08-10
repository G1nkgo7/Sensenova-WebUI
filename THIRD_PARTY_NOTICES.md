# Third-party notices

This package installs Python dependencies declared in `studio/pyproject.toml` and
`inference/pyproject.toml`. Their license texts and metadata are available from the
corresponding upstream projects and from the installed environment.

The bundled sn-ppt-web includes:

- Apache ECharts runtime assets, distributed under the Apache License 2.0.
- 45 allow-listed OFL/open-source presentation fonts for offline installation,
  stored in `bundled/fonts/`. The bundled license text is retained at both
  `bundled/fonts/OFL-1.1.txt` and
  `bundled/static-ppt-skill-suite/skills/sn-ppt-web/assets/licenses/OFL-1.1.txt`.
  The release now includes the registered Smiley Sans and IBM Plex Sans faces;
  it does not claim or reference unbundled Inter, JetBrains Mono, or Source Han
  Sans/Serif faces as delivery fonts.

The generated presentation may incorporate user-supplied or remotely retrieved material.
The deployer is responsible for ensuring that such material may lawfully be used and
distributed.
