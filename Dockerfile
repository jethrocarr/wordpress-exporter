FROM ubuntu:latest

RUN apt-get update
RUN DEBIAN_FRONTEND=noninteractive apt-get install -y \
  -o Dpkg::Options::="--force-confdef" \
  -o Dpkg::Options::="--force-confold" \
  pandoc python3 python3-pip texlive-latex-base texlive-latex-extra

RUN python3 -m pip install --break-system-packages beautifulsoup4 requests html2text 

COPY --chmod=0755 script.py /usr/local/bin/wordpress-export

WORKDIR /data

