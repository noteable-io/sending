# Changelog
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]
### Changed
- Created a separate method to get a DetachedPubSubSession

### Removed
- Dependency on `prometheus-client`

### Quality
- Set up nox

## [0.1.1] - 2021-10-15
### Added
- The ability to create isolated pubsub sessions that do not receive manager-level subscription messages

### Fixed
- Pubsub sessions no longer receive messages subscribed to in other sessions

## [0.1.0] - 2021-09-10
### Added
- Initial project scaffolding
