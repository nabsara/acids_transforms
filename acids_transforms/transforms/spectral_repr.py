from numbers import Real
from enum import Enum
from .base import AudioTransform, InversionEnumType 
from ..utils.misc import *
from .norm import Normalize, NormalizeMode
from typing import Union, Tuple
import torch
import torch.nn as nn

__all__ = ["Real", "Imaginary", "Magnitude", "IFMethods",
           "Phase", "IF", "Cartesian", "Polar", "PolarIF"]


class Dummy(AudioTransform):
    pass


class _Representation(AudioTransform):
    realtime = False
    scriptable = True
    needs_scaling = True

    def __init__(self, mode: Union[NormalizeMode, None] = NormalizeMode.UNIPOLAR):
        super().__init__()
        if mode is not None:
            self.norm = Normalize(mode)
        else:
            self.norm = Dummy()

    @property
    def scriptable(self):
        return True

    @property
    def invertible(self):
        return True

    @property
    def needs_scaling(self):
        return False

    @torch.jit.export
    def scale_data(self, x: torch.Tensor) -> None:
        return self.norm.scale_data(x)

    @torch.jit.export
    def invert(self, x, inversion_mode: InversionEnumType = None, tolerance: float = 1.e-4) -> torch.Tensor:
        return self.norm.invert(x)

    @classmethod
    def test_scripted_transform(cls, transform):
        shape = (2, 10, 513)
        complex_random = torch.randn(
            *shape) * torch.exp(2 * torch.pi * torch.rand(*shape) * torch.full(shape, 1j))
        transform.scale_data(complex_random)
        x_repr = transform(complex_random)
        if cls.invertible:
            x_inv = transform.invert(x_repr)


class Real(_Representation):
    realtime = True

    def __repr__(self):
        return "Real(norm=%s)"%self.norm.mode

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.norm(x.real)

    @torch.jit.export
    def scale_data(self, x: torch.Tensor) -> None:
        return self.norm.scale_data(x.real)

    def test_inversion(self, x: torch.Tensor):
        # simulate STFT
        x = torch.stft(x, 512, 128, return_complex=True)
        imag = x.imag
        self.scale_data(x)
        x_real = self(x)
        x_real_inv = self.invert(x_real)
        x_inv_fft = x_real_inv + imag * 1j
        x_inv = torch.istft(x_inv_fft, 512, 128)
        return {'direct': x_inv}


class Imaginary(_Representation):
    realtime = True

    def __repr__(self):
        return "Imaginary(norm=%s)"%self.norm.mode

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        if torch.is_complex(x):
            return self.norm(x.imag)
        else:
            return torch.zeros_like(x)

    @torch.jit.export
    def scale_data(self, x: torch.Tensor) -> None:
        return self.norm.scale_data(x.imag)

    def test_inversion(self, x: torch.Tensor):
        # simulate STFT
        x = torch.stft(x, 1024, 256, return_complex=True).transpose(-2,-1)
        real = x.real
        self.scale_data(x)
        x_imag = self(x)
        x_imag_inv = self.invert(x_imag)
        x_inv_fft = real + x_imag_inv * 1j
        x_inv = torch.istft(x_inv_fft.transpose(-2,-1), 1024, 256)
        return {'direct': x_inv}


class ContrastMode(Enum):
    NONE = 0
    LOG = 1
    LOG1P = 2


class Magnitude(_Representation):

    def __repr__(self):
        if self.mel:
            return "Magnitude(mel=%s, n_fft=%s, norm=%s)"%(self.mel, self.n_fft, self.norm.mode)
        else:
            return "Magnitude(norm=%s)"%self.norm.mode

    def __init__(self, 
                 mode: Union[NormalizeMode, None] = NormalizeMode.UNIPOLAR,
                 contrast: ContrastMode = ContrastMode.LOG1P,
                 mel: bool = True,
                 n_fft: int = 1024,
                 sample_rate: int = 44100,
                 dtype: torch.dtype = None, 
                 eps: float = None):
        super().__init__(mode)
        self.contrast_mode = contrast
        self.mel = mel
        self.keep_nyquist = True
        if dtype is None:
            dtype = torch.get_default_dtype()
        if eps is None:
            eps = torch.finfo(dtype).eps
        self.register_buffer("eps", torch.tensor(eps))
        if mel:
            assert sample_rate is not None
            assert n_fft is not None
            n_bins = n_fft // 2 + 1
            fft_scale = torch.arange(n_fft // 2 + 1) / n_fft * sample_rate
            if not self.keep_nyquist:
                fft_scale = fft_scale[..., 1:]
            mel_bank = torchaudio.functional.melscale_fbanks(n_bins, fft_scale[0], fft_scale[-1], n_bins, sample_rate)
            mel_norm = mel_bank.sum(0)
            mel_bank_norm = mel_bank / torch.where(mel_norm != 0, mel_norm, torch.ones_like(mel_norm)).unsqueeze(0)
            mel_inv_norm = mel_bank.sum(1)
            mel_bank_inv_norm = mel_bank / torch.where(mel_inv_norm != 0, mel_inv_norm, torch.ones_like(mel_inv_norm)).unsqueeze(1)
            self.register_buffer("mel_bank", mel_bank_norm.unsqueeze(0))
            self.register_buffer("inverse_mel_bank", mel_bank_inv_norm.transpose(-2,-1).unsqueeze(0))


    def contrast(self, mag: torch.Tensor) -> torch.Tensor:
        if self.contrast_mode == ContrastMode.LOG1P:
            return torch.log(1 + mag)
        elif self.contrast_mode == ContrastMode.LOG:
            return torch.log(torch.clamp(mag, self.eps, None))
        elif self.contrast_mode == ContrastMode.NONE:
            return mag
        else:
            raise TypeError("unknown contrast type %s"%self.contrast_mode)

    def invert_contrast(self, mag: torch.Tensor) -> torch.Tensor:
        if self.contrast_mode == ContrastMode.LOG1P:
            return torch.exp(mag) - 1
        elif self.contrast_mode == ContrastMode.LOG:
            return torch.exp(mag) - self.eps
        elif self.contrast_mode == ContrastMode.NONE:
            return mag
        else:
            raise TypeError("unknown contrast type %s"%self.contrast_mode)


    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        mag = x.abs()
        if self.mel:
            if mag.ndim == 3:
                mag = torch.bmm(mag, self.mel_bank.repeat(mag.size(0), 1, 1))
            else:
                mag = torch.matmul(mag, self.mel_bank)
        mag = self.contrast(mag)
        mag = self.norm(mag)
        return mag

    @torch.jit.export
    def invert(self, x: torch.Tensor, inversion_mode: InversionEnumType = None, tolerance: float = 1.e-4) -> torch.Tensor:
        mag = self.norm.invert(x)
        mag = self.invert_contrast(mag)
        if self.mel:
            if mag.ndim == 3:
                mag = torch.bmm(mag, self.inverse_mel_bank.repeat(mag.size(0), 1, 1))
            else:
                mag = torch.matmul(mag, self.inverse_mel_bank)
        return mag

    @torch.jit.export
    def scale_data(self, x: torch.Tensor) -> None:
        return self.norm.scale_data(x.abs())

    def test_inversion(self, x: torch.Tensor):
        # simulate STFT
        x = torch.stft(x, 1024, 256, return_complex=True).transpose(-2,-1)
        angle = x.angle()
        self.scale_data(x)
        x_mag = self(x)
        x_mag_inv = self.invert(x_mag)
        x_inv_fft = x_mag_inv * torch.exp(angle * 1j)
        x_inv = torch.istft(x_inv_fft.transpose(-2,-1), 1024, 256)
        return {'direct': x_inv}


class Phase(_Representation):
    realtime = True

    def __repr__(self):
        return "Phase(norm=%s)"%self.norm.mode

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.norm(x.angle())

    @torch.jit.export
    def scale_data(self, x: torch.Tensor) -> None:
        return self.norm.scale_data(x.angle())

    def test_inversion(self, x: torch.Tensor):
        # simulate STFT
        x = torch.stft(x, 1024, 256, return_complex=True)
        mag = x.abs()
        self.scale_data(x)
        x_angle = self(x)
        x_angle_inv = self.invert(x_angle)
        x_inv_fft = mag * torch.exp(x_angle_inv * 1j)
        x_inv = torch.istft(x_inv_fft, 1024, 256)
        return {'direct': x_inv}


class IFMethods(Enum):
    FORWARD = 1
    BACKWARD = 2
    CENTRAL = 3


class IF(_Representation):

    def __repr__(self):
        return "IF(method=%s, norm=%s)"%(self.method, self.norm.mode)

    def __init__(self, mode: Union[NormalizeMode, None] = NormalizeMode.GAUSSIAN, method=IFMethods.FORWARD, weighted=False):
        super().__init__(mode)
        self.method = method
        self.weighted = weighted
        self.weighted_window = torch.zeros(0, 0)
        self.register_buffer("eps", torch.tensor(torch.finfo(torch.float32).eps))

    def get_if(self, data: torch.Tensor):

        phase = unwrap(data.angle())
        if self.method == IFMethods.BACKWARD:
            inst_f = fdiff_backward(phase)
            inst_f[1:] /= (-torch.pi)
        elif self.method == IFMethods.FORWARD:
            inst_f = fdiff_forward(phase)
            inst_f[:-1] /= (torch.pi)
        elif self.method == IFMethods.CENTRAL:
            inst_f = fdiff_central(phase)
            inst_f[1:-1] /= (2 * torch.pi)
        else:
            raise AttributeError("method %s not known" % self.method)
        if self.weighted:
            window = self._get_weighted_window(inst_f)
            inst_f = window * inst_f
        return inst_f

    def _get_weighted_window(self, x: torch.Tensor) -> torch.Tensor:
        N = x.size(-2)
        if self.weighted_window.size(-2) != x.size(-2):
            n = torch.arange(N)
            self.weighted_window = (
                1.5 * N) / (N ** 2 - 1) * (1 - ((n - (N / 2 - 1)) / (N / 2)) ** 2)
        window_shape = [1] * x.ndim
        window_shape[-2] = N
        return self.weighted_window.view(window_shape)

    @torch.jit.export
    def scale_data(self, x: torch.Tensor):
        self.norm.scale_data(self.get_if(x))

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        inst_f = self.get_if(x)
        return self.norm(inst_f)

    @torch.jit.export
    def invert(self, x, inversion_mode: InversionEnumType = None, tolerance: float = 1.e-4) -> torch.Tensor:
        x_denorm = self.norm.invert(x)
        if self.method == IFMethods.BACKWARD:
            x_denorm[1:] *= -torch.pi
            x_denorm = fint_backward(x_denorm)
        if (self.method == IFMethods.FORWARD):
            x_denorm[:-1] *= torch.pi
            x_denorm = fint_forward(x_denorm)
        elif self.method == IFMethods.CENTRAL:
            x_denorm[1:-1] *= torch.pi
            x_denorm = fint_central(x_denorm)
        return x_denorm

    def test_inversion(self, x: torch.Tensor):
        # simulate STFT
        x = torch.stft(x, 1024, 256, return_complex=True).transpose(-2, -1)
        mag = x.abs()
        angle = x.angle()
        self.scale_data(x)
        x_angle = self(x)
        x_angle_inv = self.invert(x_angle)
        x_inv_fft = mag * torch.exp(x_angle_inv * 1j)
        x_inv = torch.istft(x_inv_fft.transpose(-2, -1), 1024, 256)
        return {'direct': x_inv}


SpectralRepresentationType = Union[torch.Tensor,
                                   Tuple[torch.Tensor, torch.Tensor]]


class SpectralRepresentation(AudioTransform):
    invertible = True
    scriptable = True
    realtime = True

    @property
    def scriptable(self):
        return True

    @property
    def invertible(self):
        return True

    @property
    def needs_scaling(self):
        return False

    def __init__(self,
                 magnitude_transform=None, phase_transform=None,
                 magnitude_mode=NormalizeMode.UNIPOLAR, phase_mode=NormalizeMode.GAUSSIAN, stack=-2):
        super().__init__()
        if type(self) == SpectralRepresentation:
            raise RuntimeError(
                "SpectralRepresentation should not be called directly.")
        self.magnitude = magnitude_transform(magnitude_mode)
        self.phase = phase_transform(phase_mode)
        self.stack = stack

    @torch.jit.export
    def scale_data(self, x: torch.Tensor) -> None:
        self.magnitude.scale_data(x)
        self.phase.scale_data(x)

    @torch.jit.export
    def forward(self, x: torch.Tensor) -> SpectralRepresentationType:
        magnitude = self.magnitude(x)
        phase = self.phase(x)
        if self.stack is not None:
            return torch.stack([magnitude, phase], dim=self.stack)
        else:
            return (magnitude, phase)

    @torch.jit.export
    def invert(self, x, inversion_mode: InversionEnumType = None, tolerance: float = 1.e-4) -> torch.Tensor:
        if self.stack is None:
            mag = x[0]
            phase = x[1]
        else:
            mag = x.index_select(
                self.stack, torch.tensor(0)).squeeze(self.stack)
            phase = x.index_select(
                self.stack, torch.tensor(1)).squeeze(self.stack)
        mag = self.magnitude.invert(mag)
        phase = self.phase.invert(phase)
        return mag * torch.exp(phase * torch.full(phase.shape, 1j, device=phase.device))

    def test_inversion(self, x: torch.Tensor):
        # simulate STFT
        x = torch.stft(x, 1024, 256, return_complex=True).transpose(-1,-2)
        self.scale_data(x)
        x_t = self(x)
        x_t_inv = self.invert(x_t)
        x_inv = torch.istft(x_t_inv.transpose(-1,-2), 1024, 256)
        return {'direct': x_inv}

    @classmethod
    def test_scripted_transform(cls, transform):
        complex_random = torch.randn(
            2, 10, 513) * torch.exp(2 * torch.pi * torch.rand(2, 10, 513) * torch.full((2, 10, 513), 1j))
        transform.scale_data(complex_random)
        x_repr = transform(complex_random)
        if cls.invertible:
            x_inv = transform.invert(x_repr)


class Cartesian(SpectralRepresentation):
    realtime = True
    def __repr__(self):
        return "Cartesian(real_norm=%s, imag_norm=%s)"%(self.magnitude.norm.mode, self.phase.norm.mode)

    def __init__(self, real_mode=NormalizeMode.GAUSSIAN, imag_mode=NormalizeMode.GAUSSIAN, stack=-2):
        super(Cartesian, self).__init__(Real, Imaginary,
                                        real_mode, imag_mode, stack=stack)


class Polar(SpectralRepresentation):
    realtime = True

    def __repr__(self):
        return "Polar(real_norm=%s, imag_norm=%s)"%(self.magnitude.norm.mode, self.phase.norm.mode)
    def __init__(self, magnitude_mode=NormalizeMode.UNIPOLAR, phase_mode=NormalizeMode.GAUSSIAN, stack=-2):
        super(Polar, self).__init__(Magnitude, Phase,
                                    magnitude_mode, phase_mode, stack=stack)


class PolarIF(SpectralRepresentation):
    realtime = True
    def __repr__(self):
        return "PolarIF(real_norm=%s, imag_norm=%s)"%(self.magnitude.norm.mode, self.phase.norm.mode)
    def __init__(self, magnitude_mode=NormalizeMode.UNIPOLAR, phase_mode=NormalizeMode.GAUSSIAN, stack=-2):
        super(PolarIF, self).__init__(Magnitude, IF,
                                      magnitude_mode, phase_mode, stack=stack)

    def test_inversion(self, x: torch.Tensor):
        # simulate STFT
        x = torch.stft(x, 1024, 256, return_complex=True).transpose(-1,-2)
        outs = {}
        for method in IFMethods:
            self.phase.method = method
            self.scale_data(x)
            x_t = self(x)
            x_t_inv = self.invert(x_t)
            x_inv = torch.istft(x_t_inv.transpose(-1,-2), 1024, 256)
            outs[method] = x_inv
        return outs
