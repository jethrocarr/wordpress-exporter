FROM ubuntu:latest

RUN apt-get update
RUN DEBIAN_FRONTEND=noninteractive apt-get install -y \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold" \
  fonts-dejavu-core pandoc python3 python3-pip texlive-latex-base \
  texlive-latex-extra texlive-xetex

RUN python3 -m pip install --break-system-packages beautifulsoup4 requests html2text pillow

COPY --chmod=0755 script.py /usr/local/bin/wordpress-export

WORKDIR /data
