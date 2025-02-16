import os
import tempfile
from pathlib import Path

import magic
import nnabla as nn
import numpy as np
import requests
import yaml
from api.separators.util import download_and_verify
from billiard.pool import Pool
from d3net.filter import apply_mwf
from d3net.separate import get_extension_context
from d3net.util import generate_data, model_separate, stft2time_domain
from django.conf import settings
from nnabla.ext_utils import get_extension_context
from spleeter.audio.adapter import AudioAdapter

MODEL_URL = 'https://github.com/JeffreyCA/spleeterweb-d3net/releases/download/d3net-mss/d3net-mss.h5'

class D3NetSeparator:
    """Performs source separation using D3Net API."""
    def __init__(
        self,
        cpu_separation: bool,
        bitrate=256
    ):
        """Default constructor.
        :param config: Separator config, defaults to None
        """
        self.model_file = 'd3net-mss.h5'
        self.model_dir = Path('pretrained_models')
        self.model_file_path = self.model_dir / self.model_file
        self.context = 'cpu' if cpu_separation else 'cudnn'
        self.bitrate = f'{bitrate}k'
        self.sample_rate = 44100
        self.audio_adapter = AudioAdapter.default()

    def get_estimates(self,
                       input_path: str,
                       parts,
                       fft_size=4096,
                       hop_size=1024,
                       n_channels=2,
                       apply_mwf_flag=True,
                       ch_flip_average=True):
        # Set NNabla extention
        ctx = get_extension_context(self.context)
        nn.set_default_context(ctx)

        # Load the model weights
        nn.load_parameters(str(self.model_file_path))

        # Read file locally
        if settings.DEFAULT_FILE_STORAGE == 'api.storage.FileSystemStorage':
            _, inp_stft = generate_data(input_path, fft_size,
                                                  hop_size, n_channels, self.sample_rate)
        else:
            # If remote, download to temp file and load audio
            fd, tmp_path = tempfile.mkstemp()
            try:
                r_get = requests.get(input_path)
                with os.fdopen(fd, 'wb') as tmp:
                    tmp.write(r_get.content)

                _, inp_stft = generate_data(tmp_path, fft_size, hop_size,
                                            n_channels, self.sample_rate)
            finally:
                # Remove temp file
                os.remove(tmp_path)

        out_stfts = {}
        estimates = {}
        inp_stft_contiguous = np.abs(np.ascontiguousarray(inp_stft))

        # Need to compute all parts even for static mix, for mwf?
        for part in parts:
            print(f'Processing {part}...')

            with open('./config/d3net/{}.yaml'.format(part)) as file:
                # Load part specific Hyper parameters
                hparams = yaml.load(file, Loader=yaml.FullLoader)

            with nn.parameter_scope(part):
                out_sep = model_separate(
                    inp_stft_contiguous, hparams, ch_flip_average=ch_flip_average)
                out_stfts[part] = out_sep * np.exp(1j * np.angle(inp_stft))

        if apply_mwf_flag:
            out_stfts = apply_mwf(out_stfts, inp_stft)

        for part, output in out_stfts.items():
            if not parts[part]:
                continue
            estimates[part] = stft2time_domain(output, hop_size, True)

        return estimates


    def create_static_mix(self, parts, input_path: str, output_path: Path):
        download_and_verify(MODEL_URL, self.model_dir, self.model_file_path)
        estimates = self.get_estimates(input_path, parts)

        final_source = None

        for name, source in estimates.items():
            if not parts[name]:
                continue
            final_source = source if final_source is None else final_source + source

        print('Writing to MP3...')
        self.audio_adapter.save(output_path, final_source, self.sample_rate, 'mp3', self.bitrate)

    def separate_into_parts(self, input_path: str, output_path: Path):
        # Check if we downloaded a webpage instead of the actual model file
        file_exists = self.model_file_path.is_file()
        mime = None
        if file_exists:
            mime = magic.from_file(str(self.model_file_path), mime=True)

        download_and_verify(MODEL_URL,
                            self.model_dir,
                            self.model_file_path,
                            force=(file_exists and mime == 'text/html'))

        parts = {
            'vocals': True,
            'drums': True,
            'bass': True,
            'other': True
        }

        estimates = self.get_estimates(input_path, parts)

        # Export all source MP3s in parallel
        pool = Pool()
        tasks = []
        output_path = Path(output_path)

        for name, estimate in estimates.items():
            filename = f'{name}.mp3'
            print(f'Exporting {name} MP3...')
            task = pool.apply_async(self.audio_adapter.save, (output_path / filename, estimate, self.sample_rate, 'mp3', self.bitrate))
            tasks.append(task)

        pool.close()
        pool.join()
