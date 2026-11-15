"""聴感の原因を取り違えないため、交換対象・固定成分・元入力を検査する。"""
import unittest
import numpy as np
from world_timbre_diagnosis import components


class TimbreTests(unittest.TestCase):
    def test_swaps_only_requested_components_and_never_pitch(self):
        teacher=np.arange(62*10,dtype=np.float32).reshape(62,10)
        predicted=-teacher-1
        f0=np.array([0,100,120,140,0,220,230,240,250,0.])
        before=(teacher.copy(),predicted.copy(),f0.copy())
        pairs=components(teacher,predicted,f0)
        for _,pitch in pairs.values():
            np.testing.assert_array_equal(pitch,f0)
        np.testing.assert_array_equal(pairs['teacher_envelope'][0][:60],teacher[:60])
        np.testing.assert_array_equal(pairs['teacher_envelope'][0][60:],predicted[60:])
        np.testing.assert_array_equal(pairs['teacher_aperiodicity'][0][:60],predicted[:60])
        np.testing.assert_array_equal(pairs['teacher_aperiodicity'][0][60:],teacher[60:])
        pairs['model_all'][0][:]=0
        pairs['teacher_timbre'][1][:]=0
        for actual,expected in zip((teacher,predicted,f0),before):
            np.testing.assert_array_equal(actual,expected)

    def test_rejects_invalid_shapes_or_values(self):
        x=np.zeros((62,10)); f=np.ones(10)*100
        for values in ((x[:61],x,f),(x,x,f[:9]),(x,x,-f),(x+np.nan,x,f)):
            with self.assertRaises(ValueError):components(*values)


if __name__=='__main__':unittest.main()
