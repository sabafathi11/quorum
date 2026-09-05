# The unified SAM service: one predictor, serving the interactor, the tracker
# and the text-prompt auto-annotator.
#
# FROM the SAM 3.1 image rather than rebuilding it — torch, the sam3 library and
# the 4 GB checkpoint are already in there, and pulling them again to make a
# near-identical image would cost half an hour and several GB of a disk that is
# 97% full. This layer is three files.
#
# `annotator/` is bind-mounted rather than copied, for the same reason
# `plugins/` is in Quorum's own compose: it is a verbatim vendored copy that
# people edit, and editing it should mean a restart rather than a rebuild.
ARG SAM_IMAGE=cvat.pth.comfyorg.sam3.1:latest-gpu
FROM ${SAM_IMAGE}

# The pipeline decodes video with OpenCV and this base has the codecs; nothing
# else is needed. Deliberately no pip install: adding a package here risks
# moving torch, and the whole value of this image is that it is the one the
# model was measured on.
WORKDIR /service
COPY server.py model_handler.py /service/

ENV PYTHONPATH=/opt/nuclio/sam:/service \
    PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
    SAM_PORT=8080 \
    SAM_ANNOTATOR=/annotator

EXPOSE 8080

# The model takes ~40 s to load; until it has, /health does not answer, which is
# exactly what a start-period is for.
HEALTHCHECK --interval=20s --timeout=5s --start-period=180s \
  CMD python3 -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8080/health',timeout=4)" || exit 1

ENTRYPOINT ["python3", "/service/server.py"]
