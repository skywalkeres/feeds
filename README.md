Feeds de texto completo para Feedly
Genera, cada 30 minutos y sin servidores propios, una versión de tus feeds con el artículo completo.
Todo corre en GitHub (Actions + Pages), gratis.
Puesta en marcha (una sola vez)
Crea un repositorio público en GitHub (por ejemplo `feeds`) y sube todo el contenido de esta carpeta
(incluida la carpeta oculta `.github`).
En el repositorio: Settings → Pages → Build and deployment: Source = Deploy from a branch,
Branch = `main`, carpeta `/docs`.
En la pestaña Actions, abre "Actualizar feeds de texto completo" y pulsa Run workflow
(la primera vez no se lanza solo).
Cuando termine, abre `https://TU_USUARIO.github.io/NOMBRE_REPO/`: verás la lista de feeds generados.
Usarlos en Feedly
Copia la URL de cada feed generado y añádela en Feedly, o
Descarga `feeds.opml` desde esa misma página e impórtalo en Feedly (web: Organize / Import OPML).
Añadir más webs
Edita `feeds.txt` (una URL de RSS/Atom por línea) y haz commit. El workflow se ejecuta solo al cambiar el archivo.
Si dejas de ver un feed antiguo, no pasa nada: puedes borrar la línea y el feed deja de actualizarse.
Ajustes (en `build_feeds.py`)
`MAX_ENTRIES`: artículos por feed (20).
`MIN_FULL_CHARS`: si el feed ya trae más texto que esto, no se descarga el artículo (1500).
`MAX_TRIES`: reintentos por artículo cuando la extracción falla (3).
Notas
El repositorio es público, así que el texto completo queda accesible en la web. Úsalo para tu lectura personal.
Si GitHub desactiva alguna vez el programador por inactividad, reactívalo desde la pestaña Actions.
Webs con muro de pago, que cargan el texto con JavaScript o que bloquean descargas automáticas pueden no extraerse bien;
en esos casos el feed conserva el extracto original.
