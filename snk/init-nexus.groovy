// 검증(verify)용 Gradle init script: 모든 Maven 저장소를 Nexus 하나로 바꿔치기한다.
// 환경 변수: BUNDLER_NEXUS_URL, BUNDLER_NEXUS_USER, BUNDLER_NEXUS_PASSWORD
import org.gradle.api.artifacts.dsl.RepositoryHandler
import org.gradle.api.artifacts.repositories.MavenArtifactRepository

def nexusUrl = System.getenv('BUNDLER_NEXUS_URL')
def nexusUser = System.getenv('BUNDLER_NEXUS_USER')
def nexusPass = System.getenv('BUNDLER_NEXUS_PASSWORD')

if (!nexusUrl) {
    throw new GradleException('BUNDLER_NEXUS_URL 환경 변수가 필요합니다.')
}

def mirror = { RepositoryHandler handler ->
    handler.all { repo ->
        if (repo instanceof MavenArtifactRepository) {
            repo.url = nexusUrl
            if (nexusUrl.startsWith('http://')) {
                repo.allowInsecureProtocol = true
            }
            if (nexusUser) {
                repo.credentials { creds ->
                    creds.username = nexusUser
                    creds.password = nexusPass
                }
            }
        }
    }
}

gradle.beforeSettings { settings ->
    mirror(settings.pluginManagement.repositories)
    try {
        mirror(settings.dependencyResolutionManagement.repositories)
    } catch (Throwable ignored) {
    }
}

gradle.allprojects { project ->
    mirror(project.repositories)
    mirror(project.buildscript.repositories)
}
