// sneakernet 용 Gradle init script.
// 모든 프로젝트의 해석 가능한 configuration(compile/runtime/test/annotationProcessor/buildscript 등)을
// 강제로 해석해서 Gradle 캐시에 모든 아티팩트를 내려받게 한다.
//
// 입력 시스템 속성:
//   bundler.out      결과 파일을 쓸 디렉토리
//   bundler.repo.N   빌드에 저장소가 하나도 선언되지 않았을 때 쓸 기본 저장소 URL (N = 0, 1, 2 ...)
// 출력 파일(bundler.out 아래):
//   gradle-requested.txt   요청된(탈락했을 수도 있는) 모든 g:a:v
//   gradle-direct.txt      각 configuration 루트의 직접 의존성(해석 결과 g:a:v)
//   gradle-selected.txt    해석 결과로 선택된 모든 모듈 g:a:v (requested 에서 빼면 탈락 버전)
//   gradle-unresolved.txt  해석에 실패한 의존성
//   gradle-failures.txt    configuration 해석 중 발생한 예외

import org.gradle.api.artifacts.component.ModuleComponentIdentifier
import org.gradle.api.artifacts.component.ModuleComponentSelector
import org.gradle.api.artifacts.dsl.RepositoryHandler
import org.gradle.api.artifacts.result.ResolvedDependencyResult
import org.gradle.api.artifacts.result.UnresolvedDependencyResult

def outDir = new File(System.getProperty('bundler.out') ?: '.')
def repoUrls = []
for (int i = 0; System.getProperty("bundler.repo.${i}") != null; i++) {
    repoUrls << System.getProperty("bundler.repo.${i}")
}

def addRepos = { RepositoryHandler handler ->
    repoUrls.eachWithIndex { String u, int idx ->
        handler.maven { repo ->
            repo.name = "bundler${idx}"
            repo.url = u
            if (u.startsWith('http://')) {
                repo.allowInsecureProtocol = true
            }
        }
    }
}

def appendLines = { String name, Collection lines ->
    if (lines.isEmpty()) return
    synchronized (outDir) {
        outDir.mkdirs()
        new File(outDir, name).withWriterAppend('UTF-8') { w -> lines.each { w.println(it) } }
    }
}

def state = [drmHasRepos: false]

gradle.settingsEvaluated { settings ->
    try {
        def pm = settings.pluginManagement.repositories
        if (pm.isEmpty()) addRepos(pm)
    } catch (Throwable t) {
        logger.warn("[bundler] pluginManagement 저장소 설정 실패: ${t.message}")
    }
    try {
        state.drmHasRepos = !settings.dependencyResolutionManagement.repositories.isEmpty()
    } catch (Throwable ignored) {
    }
}

gradle.allprojects { project ->
    project.afterEvaluate {
        try {
            if (project.repositories.isEmpty() && !state.drmHasRepos) addRepos(project.repositories)
        } catch (Throwable t) {
            logger.warn("[bundler] ${project.path} 기본 저장소 설정 실패: ${t.message}")
        }
    }

    project.tasks.register('bundlerResolveAll') {
        doLast {
            def requested = new TreeSet<String>()
            def direct = new TreeSet<String>()
            def selected = new TreeSet<String>()
            def unresolved = new TreeSet<String>()
            def failures = new TreeSet<String>()

            [project.configurations, project.buildscript.configurations].each { container ->
                new ArrayList(container).each { cfg ->
                    if (!cfg.canBeResolved) return
                    try {
                        def rr = cfg.incoming.resolutionResult
                        rr.allComponents.each { comp ->
                            def cid = comp.id
                            if (cid instanceof ModuleComponentIdentifier) {
                                selected << "${cid.group}:${cid.module}:${cid.version}".toString()
                            }
                        }
                        rr.allDependencies.each { dep ->
                            if (dep instanceof UnresolvedDependencyResult) {
                                unresolved << "${dep.requested.displayName} (${project.path}:${cfg.name}) : ${dep.failure.message?.readLines()?.getAt(0)}".toString()
                            }
                            def sel = dep.requested
                            if (sel instanceof ModuleComponentSelector) {
                                def vc = sel.versionConstraint
                                def ver = vc.strictVersion ?: vc.requiredVersion ?: vc.preferredVersion
                                if (ver && ver ==~ /[A-Za-z0-9_.\-]+/ && !ver.startsWith('latest.')) {
                                    requested << "${sel.group}:${sel.module}:${ver}".toString()
                                }
                            }
                        }
                        rr.root.dependencies.each { dep ->
                            if (dep instanceof ResolvedDependencyResult && dep.selected.id instanceof ModuleComponentIdentifier) {
                                def id = dep.selected.id
                                direct << "${id.group}:${id.module}:${id.version}".toString()
                            }
                        }
                        // 실제 파일 다운로드. lenient 로 계속 진행하되, 받지 못한 아티팩트는 실패로 기록한다.
                        def collection = cfg.incoming.artifactView { view -> view.lenient(true) }.artifacts
                        collection.artifacts
                        collection.failures.each { f ->
                            unresolved << "artifact (${project.path}:${cfg.name}) : ${f.message?.readLines()?.getAt(0)}".toString()
                        }
                    } catch (Throwable t) {
                        failures << "${project.path}:${cfg.name} ${t.class.simpleName}: ${t.message?.readLines()?.getAt(0)}".toString()
                    }
                }
            }

            appendLines('gradle-requested.txt', requested)
            appendLines('gradle-direct.txt', direct)
            appendLines('gradle-selected.txt', selected)
            appendLines('gradle-unresolved.txt', unresolved)
            appendLines('gradle-failures.txt', failures)
        }
    }
}
