# rocco-pulse — macOS menubar app build/test/install
#
# `make install` is what humans should run after they've set up the
# Rocco-side agent and their ~/.ssh/config (see README.md).
PROJECT := rocco-pulse.xcodeproj
SCHEME_CORE := RoccoPulseCore
SCHEME_APP  := RoccoPulseApp
DERIVED := build
APP_PATH := $(DERIVED)/Build/Products/Release/rocco-pulse.app
INSTALL_DEST := /Applications/rocco-pulse.app

XCODEGEN := $(shell command -v xcodegen 2>/dev/null)

.PHONY: prereqs generate test build release install clean help

help:
	@echo "Targets:"
	@echo "  make generate   regenerate the Xcode project from project.yml"
	@echo "  make test       run RoccoPulseCoreTests"
	@echo "  make build      build the app (Debug)"
	@echo "  make release    build the app (Release)"
	@echo "  make install    Release build + copy to /Applications + open"
	@echo "  make clean      wipe derived data"

prereqs:
ifndef XCODEGEN
	@echo "error: xcodegen not found on PATH."
	@echo "Install it with: brew install xcodegen"
	@exit 1
endif

generate: prereqs
	xcodegen generate

test: generate
	xcodebuild -project $(PROJECT) -scheme $(SCHEME_CORE) test \
		CODE_SIGNING_ALLOWED=NO -derivedDataPath $(DERIVED)

build: generate
	xcodebuild -project $(PROJECT) -scheme $(SCHEME_APP) -configuration Debug build \
		CODE_SIGNING_ALLOWED=NO -derivedDataPath $(DERIVED)

release: generate
	xcodebuild -project $(PROJECT) -scheme $(SCHEME_APP) -configuration Release build \
		CODE_SIGNING_ALLOWED=NO -derivedDataPath $(DERIVED)

install: release
	@if [ ! -d "$(APP_PATH)" ]; then \
		echo "error: $(APP_PATH) not found"; exit 1; \
	fi
	@if [ -d "$(INSTALL_DEST)" ]; then \
		echo "→ removing existing $(INSTALL_DEST)"; \
		rm -rf "$(INSTALL_DEST)"; \
	fi
	cp -R "$(APP_PATH)" "$(INSTALL_DEST)"
	@echo "→ installed to $(INSTALL_DEST)"
	open "$(INSTALL_DEST)"

clean:
	rm -rf $(DERIVED) $(PROJECT)
