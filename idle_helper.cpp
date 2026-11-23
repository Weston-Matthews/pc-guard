// Long-running user-session helper: KDE KIdleTime (Wayland or X11).
//
// KIdleTime::idleTime() often returns 0 on the KWin Wayland plugin, and
// logind IdleHint may never be set. The timeoutReached and resumingFromIdle
// signals do work, so this process listens for them and writes a state file:
//   $XDG_RUNTIME_DIR/pc-guard/idle.json
//
// idle_ms is exact after we have seen a resume followed by a new idle
// period. If the helper starts while the session is already idle, idle_ms
// is only a lower bound (time since the first timeout).

#include <KIdleTime>
#include <QGuiApplication>
#include <QTimer>
#include <QFile>
#include <QDir>
#include <QDateTime>
#include <csignal>
#include <iostream>
#include <cstdlib>
#include <unistd.h>

static QString statePath()
{
    QByteArray dir = qgetenv("XDG_RUNTIME_DIR");
    if (dir.isEmpty()) {
        dir = QByteArray("/run/user/") + QByteArray::number(getuid());
    }
    QString d = QString::fromLocal8Bit(dir) + "/pc-guard";
    QDir().mkpath(d);
    return d + "/idle.json";
}

// 0 = unknown / starting, 1 = compositor reports idle, 2 = recent input
static int g_state = 0;
static qint64 g_idle_origin_ms = 0; // unix-ms; idle_ms = now - origin when idle
static bool g_lower_bound = true;
static bool g_seen_resume = false;
static const int kTimeoutMs = 1000;

static void writeState()
{
    const qint64 now = QDateTime::currentMSecsSinceEpoch();
    qint64 idle_ms = 0;
    bool compositor_idle = (g_state == 1);
    if (compositor_idle && g_idle_origin_ms > 0) {
        idle_ms = now - g_idle_origin_ms;
        if (idle_ms < 0) {
            idle_ms = 0;
        }
    }
    const QString json = QString(
        "{\n"
        "  \"compositor_idle\": %1,\n"
        "  \"idle_ms\": %2,\n"
        "  \"idle_ms_is_lower_bound\": %3,\n"
        "  \"updated_at_unix\": %4,\n"
        "  \"method\": \"KIdleTime timeoutReached/resumingFromIdle\"\n"
        "}\n")
        .arg(compositor_idle ? "true" : "false")
        .arg(idle_ms)
        .arg(g_lower_bound ? "true" : "false")
        .arg(now / 1000);

    const QString path = statePath();
    const QString tmp = path + ".tmp";
    QFile f(tmp);
    if (!f.open(QIODevice::WriteOnly | QIODevice::Truncate)) {
        std::cerr << "cannot write " << tmp.toStdString() << std::endl;
        return;
    }
    f.write(json.toUtf8());
    f.close();
    QFile::remove(path);
    if (!QFile::rename(tmp, path)) {
        std::cerr << "cannot rename state file" << std::endl;
    }
}

int main(int argc, char **argv)
{
    // Prefer Wayland when a Wayland session is already visible, but do not
    // force it on X11. The systemd unit may also set QT_QPA_PLATFORM.
    if (!qEnvironmentVariableIsEmpty("WAYLAND_DISPLAY")
        && qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM")) {
        qputenv("QT_QPA_PLATFORM", "wayland");
    }
    QGuiApplication app(argc, argv);
    app.setApplicationName("pc-guard-idle");

    std::signal(SIGTERM, [](int) { QCoreApplication::quit(); });
    std::signal(SIGINT, [](int) { QCoreApplication::quit(); });

    KIdleTime *idle = KIdleTime::instance();
    idle->catchNextResumeEvent();
    idle->addIdleTimeout(kTimeoutMs);

    QObject::connect(idle, &KIdleTime::resumingFromIdle, [idle]() {
        g_state = 2;
        g_idle_origin_ms = 0;
        g_seen_resume = true;
        g_lower_bound = false; // next idle period will start from a known resume
        std::cerr << "resumed" << std::endl;
        writeState();
        idle->catchNextResumeEvent();
    });

    QObject::connect(idle, QOverload<int, int>::of(&KIdleTime::timeoutReached),
                     [](int, int timeout) {
                         if (g_state != 1) {
                             // Origin is "now minus the timeout that just fired".
                             g_idle_origin_ms =
                                 QDateTime::currentMSecsSinceEpoch() - timeout;
                             g_state = 1;
                             g_lower_bound = !g_seen_resume;
                             std::cerr << "idled timeout=" << timeout
                                       << " lower_bound=" << g_lower_bound << std::endl;
                         }
                         writeState();
                     });

    QTimer ticker;
    QObject::connect(&ticker, &QTimer::timeout, []() { writeState(); });
    ticker.start(1000);

    writeState();
    std::cerr << "pc-guard idle helper started, writing " << statePath().toStdString() << std::endl;
    return app.exec();
}
