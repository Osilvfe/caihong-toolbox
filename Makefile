BUILD_DIR ?= ../build/caihong-toolbox
DESTDIR ?=

.PHONY: check preview install

check:
	PYTHONDONTWRITEBYTECODE=1 QT_QPA_PLATFORM=offscreen python3 -m pytest -q --basetemp=$(abspath $(BUILD_DIR))/tests -o cache_dir=$(abspath $(BUILD_DIR))/pytest-cache

preview:
	python3 -m caihong_toolbox --demo

install:
	install -d $(DESTDIR)/usr/lib/caihong-toolbox/caihong_toolbox $(DESTDIR)/usr/bin
	install -m644 caihong_toolbox/*.py $(DESTDIR)/usr/lib/caihong-toolbox/caihong_toolbox/
	install -m755 bin/caihong-toolbox $(DESTDIR)/usr/bin/caihong-toolbox
	install -m755 bin/caihong-toolbox-service $(DESTDIR)/usr/lib/caihong-toolbox/caihong-toolbox-service
	install -Dm644 packaging/caihong-toolbox.service $(DESTDIR)/usr/lib/systemd/system/caihong-toolbox.service
	install -Dm644 packaging/org.caihong.Toolbox1.service $(DESTDIR)/usr/share/dbus-1/system-services/org.caihong.Toolbox1.service
	install -Dm644 packaging/org.caihong.Toolbox1.conf $(DESTDIR)/usr/share/dbus-1/system.d/org.caihong.Toolbox1.conf
	install -Dm644 packaging/org.caihong.toolbox.policy $(DESTDIR)/usr/share/polkit-1/actions/org.caihong.toolbox.policy
	install -Dm644 resources/caihong-toolbox.desktop $(DESTDIR)/usr/share/applications/caihong-toolbox.desktop
	install -Dm644 resources/caihong-toolbox.svg $(DESTDIR)/usr/share/icons/hicolor/scalable/apps/caihong-toolbox.svg
	install -Dm644 README.md $(DESTDIR)/usr/share/doc/caihong-toolbox/README.md
