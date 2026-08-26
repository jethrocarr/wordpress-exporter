# Wordpress to PDF / EPub Book Converter

Oneshot Codex generated garbage fire app.


1. Download the XML export from the Wordpress admin interface into the current dir.

2. Build and run this container

    docker build -t wexport:latest . 
    docker run --rm -it -v .:/data/ wexport:latest bash


3. Inside the container

    wordpress-export MyBlog.WordPress.2026-08-26.xml --title "Jethro Carr Blog 2007-2025" --author "Jethro Carr" --output my-blog-book --epub


Note the volume mount argument on docker, this means downloads and output files will be in the current directory. The tool downloads
all the image files from the wordpress log.
